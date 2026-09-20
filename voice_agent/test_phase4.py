#!/usr/bin/env python3
"""Phase 4 exit test: the call record and the observability seam.

Exercises the seam end to end WITHOUT placing a call and without an inference
request -- the machinery Phase 4 added is the recorder, the record, the local
JSON artifact and the sink contract, none of which need a model to be wrong.
The live console run is the human's checkpoint; this is the part that can be
asserted rather than watched.

It covers the two claims Phase 4 makes that are easy to believe and hard to
check by looking at a working call:

  1. A complete record reaches disk with the sink doing nothing at all.
  2. A sink that fails says so LOUDLY and the failure goes no further.

Run:  ./venv/bin/python test_phase4.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The patient fixtures resolve the phone number from the environment. A dummy
# value keeps this test hermetic: it needs no .env, reads no real secret, and
# never dials anything. The number is not used for anything here.
os.environ.setdefault("DESTINATION_PHONE_NUMBER", "+10000000000")
# Phase 4 is about the record and the seam, not the analysis. Switching it off
# keeps this suite free of inference calls; Phase 5's suite covers the analysis.
os.environ.setdefault("ANALYSIS_ENABLED", "false")

# setdefault would NOT be enough here, and this is not hypothetical: section 7
# runs the real entrypoint, which calls load_target_patient(), which loads .env,
# which sets OPIK_ENABLED=true -- so this suite quietly exported four junk
# "fake-job" traces to a real Opik project before this line existed. A test
# suite must not write to production telemetry. Forced, not defaulted.
os.environ["OPIK_ENABLED"] = "false"
# Section 7 constructs the real AgentSession, which validates that credentials
# are PRESENT before it will build. These are placeholders: nothing connects,
# nothing authenticates, and AgentSession.start is replaced with a no-op.
os.environ.setdefault("LIVEKIT_API_KEY", "placeholder-not-a-real-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "placeholder")
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("PATIENT_ID", "P001")

from src import agent as agent_mod
from src.events import CallRecorder, ToolInvocation, utcnow
from src.patient import get_patient
from src.sinks import EmitResult, GuardedSink, NoOpSink
from src.verification import VERIFIED, VerificationAttempt

PATIENTS_FILE = Path(__file__).resolve().parent / "data" / "patients.json"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


class RaisingSink:
    """A sink that is broken in the rudest available way."""

    name = "raising"

    def on_call_start(self, record_id: str) -> None:
        raise RuntimeError("boom on start")

    def on_call_end(self, record) -> EmitResult:
        raise RuntimeError("boom on end")

    def on_analysis(self, record, analysis) -> EmitResult:
        raise RuntimeError("boom on analysis")


class DroppingSink:
    """The Phase 6 failure mode rehearsed: no exception, just a dropped record.

    This is what an Opik flush() returning False looks like from here, and it is
    the one that produces no symptom unless something checks the return value.
    """

    name = "dropping"

    def on_call_start(self, record_id: str) -> None:
        pass

    def on_call_end(self, record) -> EmitResult:
        return EmitResult.failed("flush timed out, 3 messages dropped")

    def on_analysis(self, record, analysis) -> EmitResult:
        return EmitResult.failed("flush timed out")


class NoneReturningSink:
    """A sink written by someone who did not read the protocol."""

    name = "none-returning"

    def on_call_start(self, record_id: str) -> None:
        pass

    def on_call_end(self, record):
        return None

    def on_analysis(self, record, analysis):
        return None


def build_populated_recorder() -> CallRecorder:
    """A recorder carrying a realistic call: greeting, verification, booking."""
    patient = get_patient(PATIENTS_FILE, "P001")
    rec = CallRecorder(call_id="test-call-0001", room_name="test-room", patient=patient)
    t0 = rec.started_at
    rec.add_turn(role="assistant", text="Hello, is this Priya?", at=t0)
    rec.add_turn(role="user", text="Speaking.", at=t0 + timedelta(seconds=3))
    rec.add_turn(role="user", text="My date of birth is the 4th of March 1986.",
                 at=t0 + timedelta(seconds=9))
    rec.add_turn(role="assistant", text="Thank you, that's confirmed.",
                 at=t0 + timedelta(seconds=12))
    return rec


def verification_and_tools() -> tuple[tuple, tuple]:
    t0 = utcnow()
    attempts = (
        VerificationAttempt(
            outcome=VERIFIED, identifier_kind="date_of_birth",
            stated="the 4th of March 1986", attempt_number=0, consumed_attempt=True,
        ),
    )
    tools = (
        ToolInvocation(name="verify_patient_identity",
                       arguments={"stated_identifier": "the 4th of March 1986"},
                       result=VERIFIED, at=t0, succeeded=True),
        ToolInvocation(name="get_available_slots", arguments={}, result="3 slot(s)",
                       at=t0 + timedelta(seconds=20), succeeded=True),
        ToolInvocation(name="book_appointment", arguments={"slot_id": "S2"},
                       result="APPT-8842", at=t0 + timedelta(seconds=45), succeeded=True),
    )
    return attempts, tools


async def run(tmp: Path) -> None:
    # The real 1.5s wait is exercised in its own check below; everywhere else it
    # would just make the suite slow for no added coverage.
    agent_mod.AUDIO_WAIT_SECONDS = 0.05
    agent_mod.AUDIO_POLL_SECONDS = 0.01
    os.environ["CALL_AUDIO_DIR"] = str(tmp / "no-recordings")

    attempts, tools = verification_and_tools()

    print("\n1. A complete record reaches disk with the no-op sink")
    agent_mod.CALL_RECORDS_DIR = tmp
    rec = build_populated_recorder()
    sink = GuardedSink(NoOpSink())
    await agent_mod.finish_call(recorder=rec, sink=sink,
                                agent_holder=lambda: _FakeAgent(attempts, tools),
                                reason="user_initiated")

    path = tmp / "test-call-0001.json"
    check("JSON file written", path.exists(), str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    check("transcript present", len(data["transcript"]) == 4, f"{len(data['transcript'])} turns")
    check("tool invocations present", len(data["tool_invocations"]) == 3,
          f"{len(data['tool_invocations'])} calls")
    check("every tool call is timestamped",
          all(t["at"] for t in data["tool_invocations"]))
    check("verification attempts present", len(data["verification_attempts"]) == 1)
    check("biomarkers carried", len(data["biomarkers"]) > 0,
          f"{len(data['biomarkers'])} biomarker(s)")
    check("patient identity carried", data["patient_id"] == "P001")
    check("end_reason recorded", data["end_reason"] == "user_initiated", data["end_reason"])
    check("duration computed", isinstance(data["duration_seconds"], float))
    check("analysis slot present and empty (Phase 5 fills it)", data["analysis"] is None)
    check("expected DOB never stored",
          "1986-03-04" not in json.dumps(data),
          "verification values must not reach the record")

    print("\n1b. Call audio is attached when a recording exists")
    import time
    from datetime import datetime, timezone
    audio_dir = tmp / "recordings"
    audio_dir.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc)

    found = agent_mod.find_call_audio(audio_dir, not_before=start)
    check("no audio -> None, not an error", found is None)

    stale = audio_dir / "old-call.wav"
    stale.write_bytes(b"RIFF....WAVE")
    import os as _os
    old_t = start.timestamp() - 3600
    _os.utime(stale, (old_t, old_t))
    check("a recording from a PREVIOUS call is ignored",
          agent_mod.find_call_audio(audio_dir, not_before=start) is None)

    time.sleep(0.01)
    fresh = audio_dir / "this-call.wav"
    fresh.write_bytes(b"RIFF....WAVE")
    check("a recording from THIS call is found",
          agent_mod.find_call_audio(audio_dir, not_before=start) == fresh)

    ogg = audio_dir / "this-call.ogg"
    ogg.write_bytes(b"OggS")
    check("ogg is accepted (confirmed: lk writes audio.ogg)",
          agent_mod.find_call_audio(audio_dir, not_before=start) is not None)

    nested = audio_dir / "session-09-19-223948"
    nested.mkdir(exist_ok=True)
    time.sleep(0.01)
    (nested / "audio.ogg").write_bytes(b"OggS")
    picked = agent_mod.find_call_audio(audio_dir, not_before=start)
    check("audio NESTED in a session folder is found (lk writes it this way)",
          picked is not None and picked.parent.name.startswith("session-"),
          str(picked))

    (audio_dir / "notes.txt").write_text("not audio")
    picked = agent_mod.find_call_audio(audio_dir, not_before=start)
    check("non-audio files are ignored", picked.suffix in (".wav", ".ogg"), str(picked))

    check("a missing directory is not an error",
          agent_mod.find_call_audio(tmp / "nope", not_before=start) is None)

    # The race this wait exists for: the file lands AFTER the lookup begins.
    late_dir = tmp / "late"
    late_dir.mkdir()
    late_start = datetime.now(timezone.utc)
    agent_mod.AUDIO_WAIT_SECONDS = 2.0
    agent_mod.AUDIO_POLL_SECONDS = 0.05

    async def write_late():
        await asyncio.sleep(0.3)
        (late_dir / "session-x").mkdir(exist_ok=True)
        (late_dir / "session-x" / "audio.ogg").write_bytes(b"OggS")

    writer = asyncio.create_task(write_late())
    found_late = await agent_mod.wait_for_call_audio(late_dir, not_before=late_start)
    await writer
    check("audio written AFTER the lookup starts is still found", found_late is not None,
          str(found_late))

    t0 = asyncio.get_running_loop().time()
    none_found = await agent_mod.wait_for_call_audio(tmp / "empty-dir", not_before=late_start)
    waited = asyncio.get_running_loop().time() - t0
    check("no directory at all returns immediately, no 2s stall",
          none_found is None and waited < 0.5, f"{waited:.2f}s")
    agent_mod.AUDIO_WAIT_SECONDS = 0.05
    agent_mod.AUDIO_POLL_SECONDS = 0.01

    # End to end: the path reaches the saved record.
    agent_mod.CALL_RECORDS_DIR = tmp / "withaudio"
    _os.environ["CALL_AUDIO_DIR"] = str(audio_dir)
    try:
        # The recorder must exist BEFORE the recording is written -- its
        # started_at is the cutoff, and a file older than the call it belongs to
        # is exactly what the previous-call check rejects.
        rec = build_populated_recorder()
        rec.call_id = "with-audio"
        time.sleep(0.01)
        (audio_dir / "during-this-call.wav").write_bytes(b"RIFF....WAVE")
        await agent_mod.finish_call(recorder=rec, sink=GuardedSink(NoOpSink()),
                                    agent_holder=lambda: _FakeAgent(attempts, tools),
                                    reason="user_initiated")
        saved = json.loads((tmp / "withaudio" / "with-audio.json").read_text())
        check("audio_path lands in the saved record", saved["audio_path"] is not None,
              str(saved["audio_path"]))
    finally:
        _os.environ.pop("CALL_AUDIO_DIR", None)

    print("\n1c. A slow sink does not block the event loop")
    import asyncio as _aio

    class SlowSink:
        name = "slow"
        def on_call_start(self, record_id): time.sleep(0.4)
        def on_call_end(self, record):
            time.sleep(0.4)
            from src.sinks import EmitResult as _E
            return _E.ok()
        def on_analysis(self, record, analysis):
            from src.sinks import EmitResult as _E
            return _E.ok()

    # If the sink ran on the loop, this heartbeat would stall with it. Opik's
    # flush() is exactly this shape -- it blocks until delivery.
    ticks = 0
    async def heartbeat():
        nonlocal ticks
        try:
            while True:
                await _aio.sleep(0.05)
                ticks += 1
        except _aio.CancelledError:
            pass

    hb = _aio.create_task(heartbeat())
    agent_mod.CALL_RECORDS_DIR = tmp / "slow"
    rec = build_populated_recorder()
    rec.call_id = "slow-sink"
    await agent_mod.finish_call(recorder=rec, sink=GuardedSink(SlowSink()),
                                agent_holder=lambda: _FakeAgent(attempts, tools),
                                reason="user_initiated")
    hb.cancel(); await hb
    check("the loop kept running while the sink blocked", ticks >= 4,
          f"{ticks} heartbeats during a 0.4s blocking sink")
    check("the record was still written", (tmp / "slow" / "slow-sink.json").exists())

    print("\n2. note_end keeps the FIRST reason, not the last")
    r2 = CallRecorder(call_id="c2", room_name="r", patient=get_patient(PATIENTS_FILE, "P001"))
    r2.note_end(reason="user_initiated")
    r2.note_end(reason="job_shutdown")
    check("first reason wins", r2.build().end_reason == "user_initiated")

    print("\n3. A sink that RAISES: logged loudly, never propagates")
    guarded = GuardedSink(RaisingSink())
    guarded.on_call_start("c3")  # must not raise
    result = guarded.on_call_end(build_populated_recorder().build())
    check("exception did not propagate", True)
    check("reported as not delivered", result.delivered is False, result.detail)
    check("detail names the exception", "RuntimeError" in result.detail, result.detail)

    print("\n4. A sink that DROPS silently: caught by the return value")
    result = GuardedSink(DroppingSink()).on_call_end(build_populated_recorder().build())
    check("reported as not delivered", result.delivered is False)
    check("detail preserved", "dropped" in result.detail, result.detail)

    print("\n5. A sink that returns None: treated as failure, not success")
    result = GuardedSink(NoneReturningSink()).on_call_end(build_populated_recorder().build())
    check("None is not mistaken for success", result.delivered is False, result.detail)

    print("\n6. The record still reaches disk when the sink is broken")
    agent_mod.CALL_RECORDS_DIR = tmp / "broken"
    rec = build_populated_recorder()
    rec2_id = "test-call-0002"
    rec.call_id = rec2_id
    await agent_mod.finish_call(recorder=rec, sink=GuardedSink(RaisingSink()),
                                agent_holder=lambda: _FakeAgent(attempts, tools),
                                reason="participant_disconnected")
    check("inspection artifact survives a broken sink",
          (tmp / "broken" / f"{rec2_id}.json").exists())


class _FakeAgent:
    """Stands in for whichever agent holds the logs at the end of the call."""

    def __init__(self, attempts, tools) -> None:
        self.verification_log = list(attempts)
        self.tool_log = list(tools)


async def run_entrypoint_wiring(tmp: Path) -> None:
    """Section 7: drive the REAL entrypoint, with no room and no models.

    Sections 1-6 test the machinery through a stand-in agent. This one runs
    `agent.entrypoint` itself inside a fake job context, with
    `AgentSession.start` stubbed out, so what is asserted is the actual wiring
    a call would use -- that the shutdown callback really is registered, that
    session events really do reach the recorder, and that invoking the callback
    really does produce a complete record.

    What this CANNOT prove: that the framework fires the callback at the end of
    a real session. `ctx.shutdown()` on a fake context is a no-op -- verified,
    not assumed -- so the callback is invoked here the way the framework would.
    Only a live run closes that last gap, which is what the human checkpoint is
    for.
    """
    from livekit.agents import AgentSession, llm
    from livekit.agents.testing import fake_job_context
    from livekit.agents.voice.agent_session import CloseReason
    from livekit.agents.voice.events import CloseEvent, ConversationItemAddedEvent

    captured: dict = {}

    async def fake_start(self, agent=None, **kwargs):  # never touches a room
        captured["session"], captured["agent"] = self, agent

    real_start = AgentSession.start
    AgentSession.start = fake_start
    try:
        agent_mod.CALL_RECORDS_DIR = tmp
        with fake_job_context() as ctx:
            await agent_mod.entrypoint(ctx)

            check("entrypoint registers exactly one shutdown callback",
                  len(ctx._shutdown_callbacks) == 1)
            check("session started with the UNVERIFIED agent",
                  type(captured.get("agent")).__name__ == "UnverifiedAgent")

            session = captured["session"]
            session.emit("conversation_item_added", ConversationItemAddedEvent(
                item=llm.ChatMessage(role="assistant", content=["Hello, is this Priya?"])))
            session.emit("conversation_item_added", ConversationItemAddedEvent(
                item=llm.ChatMessage(role="user", content=["Speaking."])))
            session.emit("close", CloseEvent(reason=CloseReason.USER_INITIATED))

            # Exactly what the framework does at job shutdown.
            for cb in ctx._shutdown_callbacks:
                await cb("job_shutdown")

            path = tmp / f"{ctx.job.id}.json"
            check("record written from the real shutdown path", path.exists())
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                check("live session events reached the recorder",
                      len(data["transcript"]) == 2, f"{len(data['transcript'])} turns")
                check("close reason beat the later shutdown reason",
                      data["end_reason"] == "user_initiated", data["end_reason"])

            # Idempotency: a second invocation must not double-emit.
            before = path.read_text(encoding="utf-8") if path.exists() else ""
            for cb in ctx._shutdown_callbacks:
                await cb("job_shutdown")
            check("second shutdown is a no-op",
                  path.read_text(encoding="utf-8") == before)
    finally:
        AgentSession.start = real_start


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG, format="      [%(levelname)s] %(name)s: %(message)s"
    )
    print(__doc__.strip().split("\n")[0])
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run(Path(td)))
        print("\n7. The real entrypoint's wiring, with no room and no models")
        asyncio.run(run_entrypoint_wiring(Path(td) / "entrypoint"))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All Phase 4 exit-test checks passed.")
