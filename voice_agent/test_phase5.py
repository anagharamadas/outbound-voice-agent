#!/usr/bin/env python3
"""Phase 5 exit test: post-call analysis and the deterministic/inferred split.

No LLM call by default. The deterministic half never needed one, and the
disagreement case -- the one the exit test actually turns on -- is provoked by
injecting an analyser that claims a booking, which is exact and free. Getting a
real model to hallucinate a booking on demand is neither.

To additionally run the real model over the saved call (costs inference):

    ./venv/bin/python test_phase5.py --live

Run:  ./venv/bin/python test_phase5.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sections 1-9 need no credentials; a placeholder satisfies the patient fixture.
os.environ.setdefault("DESTINATION_PHONE_NUMBER", "+10000000000")

# --live needs LIVEKIT_API_KEY, because LiveKit Inference is this project's only
# model access. Load .env the way the agent worker does. Nothing is printed and
# nothing is dialled; if .env is absent, --live fails loudly and the rest still
# runs.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from src.analysis import (
    CallAnalysis,
    InferredAnalysis,
    OutcomeCategory,
    PatientSentiment,
    analyse_call,
    compute_facts,
    find_discrepancies,
    render_transcript,
)
from src.events import CallRecord, CallRecorder, ToolInvocation
from src.patient import get_patient
from src.verification import VERIFIED, VerificationAttempt

PATIENTS_FILE = Path(__file__).resolve().parent / "data" / "patients.json"
REAL_RECORD = Path(__file__).resolve().parent / "call_records"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def build_record(*, booking_succeeds: bool, verified: bool = True) -> CallRecord:
    """A call that sounds identical either way.

    The transcript says an appointment was made. Whether the booking TOOL
    succeeded is the variable -- which is the whole point: the conversation is
    not evidence, and this fixture makes that concrete.
    """
    patient = get_patient(PATIENTS_FILE, "P001")
    rec = CallRecorder(call_id="synthetic-1", room_name="test", patient=patient)
    t0 = rec.started_at
    rec.add_turn(role="assistant", text="This is Lakeside Family Clinic. May I speak with Meera?", at=t0)
    rec.add_turn(role="user", text="Speaking.", at=t0 + timedelta(seconds=4))
    rec.add_turn(role="user", text="March 22nd 1988.", at=t0 + timedelta(seconds=12))
    rec.add_turn(role="assistant", text="Thank you. Your HbA1c is 7.8 percent, above the target range. Would you like an appointment?", at=t0 + timedelta(seconds=15))
    rec.add_turn(role="user", text="Yes please, the Monday slot.", at=t0 + timedelta(seconds=30))
    # The agent says it is booked. In the failing variant, it is not.
    rec.add_turn(role="assistant", text="Your appointment is booked for Monday at ten with Dr. Rao.", at=t0 + timedelta(seconds=45))
    rec.note_end(reason="user_initiated")

    tools: list[ToolInvocation] = []
    attempts: list[VerificationAttempt] = []
    if verified:
        tools.append(ToolInvocation(name="verify_patient_identity",
                                    arguments={"stated_identifier": "March 22nd 1988"},
                                    result=VERIFIED, at=t0 + timedelta(seconds=14), succeeded=True))
        attempts.append(VerificationAttempt(outcome=VERIFIED, identifier_kind="date_of_birth",
                                            stated="March 22nd 1988", attempt_number=0,
                                            consumed_attempt=True))
    tools.append(ToolInvocation(name="get_available_slots", arguments={}, result="3 slot(s)",
                                at=t0 + timedelta(seconds=32), succeeded=True))
    if booking_succeeds:
        tools.append(ToolInvocation(name="book_appointment", arguments={"slot_id": "SLOT-A"},
                                    result="APT-P001-0001", at=t0 + timedelta(seconds=44),
                                    succeeded=True))
    else:
        # The failure the transcript does not reflect.
        tools.append(ToolInvocation(name="book_appointment", arguments={"slot_id": "SLOT-A"},
                                    result="backend_unavailable", at=t0 + timedelta(seconds=44),
                                    succeeded=False))
    return rec.build(verification_attempts=tuple(attempts), tool_invocations=tuple(tools))


def stub(**overrides) -> InferredAnalysis:
    """An inferred result with sensible defaults, overridable per test."""
    base = dict(
        outcome_category=OutcomeCategory.APPOINTMENT_BOOKED,
        patient_sentiment=PatientSentiment.POSITIVE,
        objection_reason=None,
        biomarkers_communicated=True,
        agent_gave_medical_advice=False,
        medical_advice_evidence=None,
        summary="The patient verified their identity, heard their results, and booked.",
    )
    base.update(overrides)
    return InferredAnalysis(**base)


def analyser_returning(result: InferredAnalysis):
    async def _a(record: CallRecord) -> InferredAnalysis:
        return result
    return _a


async def failing_analyser(record: CallRecord) -> InferredAnalysis:
    raise TimeoutError("model did not respond")


async def run() -> None:
    print("\n1. Deterministic facts, computed from the record alone")
    good = build_record(booking_succeeds=True)
    f = compute_facts(good)
    check("appointment_booked true when the tool succeeded", f.appointment_booked is True)
    check("confirmation_id taken from the tool result", f.confirmation_id == "APT-P001-0001", str(f.confirmation_id))
    check("identity_verified derived, not guessed", f.identity_verified is True)
    check("tool_call_count", f.tool_call_count == 3, str(f.tool_call_count))
    check("verification_attempts counts only consumed ones", f.verification_attempts == 1)
    check("verification_outcome", f.verification_outcome == VERIFIED, str(f.verification_outcome))

    print("\n2. THE EXIT TEST: transcript implies a booking, tool call failed")
    bad = build_record(booking_succeeds=False)
    check("the transcript still says 'booked'",
          any("booked" in t.text.lower() for t in bad.transcript if t.role == "assistant"))
    result = await analyse_call(bad, analyser=analyser_returning(
        stub(outcome_category=OutcomeCategory.APPOINTMENT_BOOKED)))
    check("appointment_booked is FALSE", result.deterministic.appointment_booked is False)
    check("confirmation_id is None", result.deterministic.confirmation_id is None)
    check("discrepancy is set", len(result.discrepancies) > 0,
          result.discrepancies[0][:70] if result.discrepancies else "none")
    check("the model's claim is NOT silently overwritten",
          result.inferred.outcome_category is OutcomeCategory.APPOINTMENT_BOOKED,
          "both values survive, disagreement recorded")

    print("\n3. The reverse disagreement is caught too")
    d = find_discrepancies(compute_facts(good),
                           stub(outcome_category=OutcomeCategory.APPOINTMENT_DECLINED))
    check("booked-but-model-says-declined is flagged", len(d) > 0, d[0][:70] if d else "none")

    print("\n4. Biomarkers claimed without verification is flagged")
    unver = build_record(booking_succeeds=False, verified=False)
    r = await analyse_call(unver, analyser=analyser_returning(
        stub(outcome_category=OutcomeCategory.IDENTITY_UNVERIFIED, biomarkers_communicated=True)))
    check("identity_verified false", r.deterministic.identity_verified is False)
    check("disclosure-without-verification flagged",
          any("disclosure incident" in x for x in r.discrepancies))

    print("\n5. Agreement produces NO discrepancy")
    r = await analyse_call(good, analyser=analyser_returning(stub()))
    check("no false positives", r.discrepancies == (), str(r.discrepancies))

    print("\n6. A failed model call does not lose the deterministic facts")
    r = await analyse_call(good, analyser=failing_analyser)
    check("facts survive", r.deterministic.appointment_booked is True)
    check("inferred is None", r.inferred is None)
    check("error recorded", "TimeoutError" in (r.inference_error or ""), r.inference_error or "")
    check("no discrepancies invented", r.discrepancies == ())

    print("\n7. The schema is closed (the model cannot invent a label)")
    from pydantic import ValidationError
    try:
        InferredAnalysis(outcome_category="rescheduled_maybe", patient_sentiment="happy",
                         objection_reason=None, biomarkers_communicated=True,
                         agent_gave_medical_advice=False, medical_advice_evidence=None,
                         summary="x")
        check("invalid enum rejected", False, "it was accepted")
    except ValidationError:
        check("invalid enum rejected", True)

    from livekit.agents.llm.utils import to_openai_response_format
    schema = to_openai_response_format(InferredAnalysis)["json_schema"]
    check("rendered as strict json_schema", schema.get("strict") is True)
    check("additionalProperties disallowed",
          schema["schema"].get("additionalProperties") is False)

    print("\n7b. A safety flag with no quoted evidence is itself flagged")
    d = find_discrepancies(compute_facts(good),
                           stub(agent_gave_medical_advice=True, medical_advice_evidence=None))
    check("unevidenced advice flag is caught", any("quoted no supporting words" in x for x in d),
          d[0][:60] if d else "none")
    d = find_discrepancies(compute_facts(good),
                           stub(agent_gave_medical_advice=True,
                                medical_advice_evidence="you should cut down on sugar"))
    check("evidenced advice flag is NOT a discrepancy", d == (), str(d))

    print("\n8. The model is shown the transcript ONLY, never the tool log")
    rendered = render_transcript(bad)
    check("transcript present", "Speaking." in rendered)
    check("booking tool result withheld", "backend_unavailable" not in rendered,
          "otherwise the model could copy the deterministic answer back")

    print("\n9. Round-trip and analyse the REAL Phase 4 record")
    saved = sorted(REAL_RECORD.glob("*.json")) if REAL_RECORD.exists() else []
    if not saved:
        print("  SKIP  no saved record in call_records/ (gitignored; run the console test)")
    else:
        rec = CallRecord.read_json(saved[-1])
        check("round-trips byte-identically",
              rec.to_dict() == json.loads(saved[-1].read_text(encoding="utf-8")))
        f = compute_facts(rec)
        check("real call: appointment_booked", f.appointment_booked is True)
        check("real call: identity_verified", f.identity_verified is True)
        check("real call: confirmation_id read from tool output",
              (f.confirmation_id or "").startswith("APT-"), str(f.confirmation_id))
        check("real call: duration", (f.call_duration_seconds or 0) > 0,
              f"{f.call_duration_seconds}s")

        if "--live" in sys.argv:
            print("\n10. LIVE: the real model over the real transcript")
            live = await analyse_call(rec)
            if live.inference_error:
                check("live analysis succeeded", False, live.inference_error)
            else:
                check("valid structured object", isinstance(live, CallAnalysis))
                print(f"        outcome    : {live.inferred.outcome_category.value}")
                print(f"        sentiment  : {live.inferred.patient_sentiment.value}")
                print(f"        biomarkers : {live.inferred.biomarkers_communicated}")
                print(f"        advice     : {live.inferred.agent_gave_medical_advice}")
                print(f"        evidence   : {live.inferred.medical_advice_evidence!r}")
                print(f"        summary    : {live.inferred.summary}")
                print(f"        discrepancies: {list(live.discrepancies) or 'none'}")
                check("deterministic booking still wins",
                      live.deterministic.appointment_booked is True)


async def run_wiring(tmp: Path) -> None:
    """Sections 11-12: the shutdown path actually runs the analysis.

    Sections 1-10 test analysis as a function. These test that something calls
    it -- which is the part that was missing, and the part Phase 4 task 5 asks
    for ("ensure on_call_end AND the Phase 5 analysis actually run and complete
    before exit").
    """
    import asyncio as _asyncio

    from src import agent as agent_mod
    from src.analysis import CallAnalysis, compute_facts
    from src.sinks import EmitResult, GuardedSink

    class SpySink:
        name = "spy"

        def __init__(self) -> None:
            self.call_end = 0
            self.analysis_payloads: list[dict] = []

        def on_call_start(self, record_id: str) -> None: ...

        def on_call_end(self, record) -> EmitResult:
            self.call_end += 1
            return EmitResult.ok()

        def on_analysis(self, record, analysis) -> EmitResult:
            self.analysis_payloads.append(analysis)
            return EmitResult.ok()

    class FakeAgent:
        def __init__(self, rec):
            self.verification_log = list(rec.verification_attempts)
            self.tool_log = list(rec.tool_invocations)

    source = build_record(booking_succeeds=True)
    agent_mod.CALL_RECORDS_DIR = tmp
    os.environ["ANALYSIS_ENABLED"] = "true"

    def recorder_for(call_id):
        patient = get_patient(PATIENTS_FILE, "P001")
        r = CallRecorder(call_id=call_id, room_name="t", patient=patient)
        for t in source.transcript:
            r.add_turn(role=t.role, text=t.text, at=t.at)
        return r

    print("\n11. The shutdown path runs the analysis and attaches it")
    real = agent_mod.analyse_call

    async def stub_analyse(record):
        return CallAnalysis(deterministic=compute_facts(record),
                            inferred=stub(), discrepancies=())

    agent_mod.analyse_call = stub_analyse
    try:
        spy = SpySink()
        await agent_mod.finish_call(recorder=recorder_for("wired-1"),
                                    sink=GuardedSink(spy),
                                    agent_holder=lambda: FakeAgent(source),
                                    reason="user_initiated")
        check("on_call_end emitted once", spy.call_end == 1, str(spy.call_end))
        check("on_analysis emitted", len(spy.analysis_payloads) == 1)
        saved = CallRecord.read_json(tmp / "wired-1.json")
        check("analysis attached to the saved record", saved.analysis is not None)
        check("deterministic half present",
              (saved.analysis or {}).get("deterministic", {}).get("appointment_booked") is True)
        check("inferred half present",
              (saved.analysis or {}).get("inferred", {}).get("outcome_category")
              == "appointment_booked")

        print("\n12. A hanging analysis is abandoned; the record survives")
        async def hanging(record):
            await _asyncio.sleep(30)
        agent_mod.analyse_call = hanging
        agent_mod.ANALYSIS_TIMEOUT_SECONDS = 0.3
        spy2 = SpySink()
        t0 = _asyncio.get_event_loop().time()
        await agent_mod.finish_call(recorder=recorder_for("wired-2"),
                                    sink=GuardedSink(spy2),
                                    agent_holder=lambda: FakeAgent(source),
                                    reason="user_initiated")
        elapsed = _asyncio.get_event_loop().time() - t0
        check("returned promptly, did not block on the hang", elapsed < 3.0, f"{elapsed:.2f}s")
        check("record still written", (tmp / "wired-2.json").exists())
        check("trace still emitted before the analysis", spy2.call_end == 1)
        check("no analysis emitted", spy2.analysis_payloads == [])
        check("record has no analysis attached",
              CallRecord.read_json(tmp / "wired-2.json").analysis is None)

        print("\n13. ANALYSIS_ENABLED=false skips it entirely")
        os.environ["ANALYSIS_ENABLED"] = "false"
        agent_mod.analyse_call = stub_analyse
        spy3 = SpySink()
        await agent_mod.finish_call(recorder=recorder_for("wired-3"),
                                    sink=GuardedSink(spy3),
                                    agent_holder=lambda: FakeAgent(source),
                                    reason="user_initiated")
        check("record still written", (tmp / "wired-3.json").exists())
        check("analysis skipped", spy3.analysis_payloads == [])
    finally:
        agent_mod.analyse_call = real
        os.environ["ANALYSIS_ENABLED"] = "true"


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s")
    print(__doc__.strip().split("\n")[0])
    import tempfile
    asyncio.run(run())
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run_wiring(Path(td)))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All Phase 5 exit-test checks passed.")
