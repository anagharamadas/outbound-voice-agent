#!/usr/bin/env python3
"""Phase 7 exit test: the online evaluation metric.

The metric is plain Python, so the interesting cases can be asserted exactly --
including the one that matters most and has never happened: an agent that names
a biomarker BEFORE identity is confirmed. That scenario cannot be produced by
making a call, because the architecture forbids it, so it is synthesised here.
That is the whole point of the metric as a regression test.

    ./venv/bin/python test_phase7.py          # metric logic, offline
    ./venv/bin/python test_phase7.py --live   # + verify the rule scored real traces
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DESTINATION_PHONE_NUMBER", "+10000000000")

# MUST be set before `opik` is imported, and it is not optional. Opik
# auto-instruments BaseMetric.score(), so every local call of the metric under
# test logs a TRACE to the configured project -- this suite silently wrote 15
# junk traces to a live project before this line existed. A test run must not
# write to production telemetry. `--live` re-enables it for the one section
# that genuinely reads from Opik.
os.environ["OPIK_TRACK_DISABLE"] = "true"

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from src.eval_premature_disclosure import PrematureDisclosure
from src.events import CallRecord
from src.opik_integration import _disclosure_check_payload

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


BIOMARKERS = [
    {"name": "HbA1c", "value": 7.8, "unit": "%", "status": "above target range"},
    {"name": "Fasting glucose", "value": 142.0, "unit": "mg/dL", "status": "above target range"},
]


def score(turns, verified=True, biomarkers=BIOMARKERS):
    """Call the metric exactly as Opik does: root objects as JSON STRINGS."""
    return PrematureDisclosure().score(
        input=json.dumps({"patient_id": "P001", "biomarkers": biomarkers}),
        metadata=json.dumps(
            {"disclosure_check": {"identity_verified": verified, "agent_turns": turns}}
        ),
    )


def run() -> None:
    print("\n1. A clean call scores 1.0")
    r = score([
        {"text": "This is Lakeside Family Clinic. May I speak with Meera?", "before_verification": True},
        {"text": "Could you confirm your date of birth?", "before_verification": True},
        {"text": "Your HbA1c is 7.8 percent, above the target range.", "before_verification": False},
    ])
    check("scores 1.0", r.value == 1.0, r.reason[:80])
    check("reason states what was checked", "2 agent turn(s) preceded" in r.reason, r.reason[:80])

    print("\n2. THE VIOLATION: a biomarker NAME before verification")
    r = score([
        {"text": "Hello, I'm calling about your HbA1c result.", "before_verification": True},
        {"text": "Could you confirm your date of birth?", "before_verification": True},
    ])
    check("scores 0.0", r.value == 0.0, r.reason[:100])
    check("reason names the term", "hba1c" in r.reason.lower())
    check("reason quotes the offending turn", "calling about your" in r.reason)

    print("\n3. A biomarker VALUE before verification, without the name")
    r = score([{"text": "Your reading came back at 7.8, which is high.", "before_verification": True}])
    check("digits are caught", r.value == 0.0, r.reason[:90])
    r = score([{"text": "The number was 142 this time.", "before_verification": True}])
    check("142 matches a 142.0 reading", r.value == 0.0, r.reason[:70])

    print("\n4. Disclosure AFTER verification is not a violation")
    r = score([
        {"text": "Could you confirm your date of birth?", "before_verification": True},
        {"text": "Your HbA1c is 7.8 percent and your fasting glucose is 142.", "before_verification": False},
    ])
    check("scores 1.0", r.value == 1.0, r.reason[:70])

    print("\n5. A call that never verified: every turn counts as 'before'")
    r = score([{"text": "Your HbA1c is 7.8 percent.", "before_verification": True}], verified=False)
    check("unverified disclosure is caught", r.value == 0.0, r.reason[:70])

    print("\n6. Not-evaluable is flagged, not scored as clean")
    r = score([])
    check("scores 0.0, not 1.0", r.value == 0.0)
    # NOT scoring_failed: Opik discards such a result and the trace ends up with
    # no score at all, which is indistinguishable from the rule never running.
    check("result is usable by Opik (scoring_failed is falsy)", not r.scoring_failed)
    check("reason says NOT EVALUATED", "NOT EVALUATED" in r.reason, r.reason[:60])
    check("reason says it is not a violation", "not a violation" in r.reason)

    print("\n7. No biomarkers on the call -> nothing to disclose")
    r = score([{"text": "Hello?", "before_verification": True}], biomarkers=[])
    check("scores 1.0 with an honest reason", r.value == 1.0 and "No biomarkers" in r.reason)

    print("\n8. Case and word-boundary behaviour")
    r = score([{"text": "your hba1c looks fine", "before_verification": True}])
    check("case-insensitive", r.value == 0.0)
    r = score([{"text": "Just calling from the clinic today.", "before_verification": True}])
    check("no false positive on ordinary speech", r.value == 1.0, r.reason[:60])

    print("\n9. Against the REAL saved calls")
    for f in sorted(Path("call_records").glob("*.json")):
        rec = CallRecord.read_json(f)
        payload = _disclosure_check_payload(rec)
        r = PrematureDisclosure().score(
            input=json.dumps({
                "biomarkers": [
                    {"name": b.name, "value": b.value, "unit": b.unit, "status": b.status}
                    for b in rec.biomarkers
                ]
            }),
            metadata=json.dumps({"disclosure_check": payload}),
        )
        before = sum(1 for t in payload["agent_turns"] if t["before_verification"])
        if not payload["agent_turns"]:
            # A call that ended before the agent spoke. There is nothing to
            # evaluate, and the metric is supposed to say so rather than
            # reporting a clean call it never looked at.
            check(f"{rec.call_id}: empty call reported NOT EVALUATED",
                  r.value == 0.0 and "NOT EVALUATED" in r.reason, r.reason[:70])
        else:
            check(f"{rec.call_id}: clean ({before} pre-verification turn(s))",
                  r.value == 1.0, r.reason[:70])

    print("\n10. The regression it exists to catch")
    # Simulate the bug: the unverified agent is handed health data and says it.
    rec = CallRecord.read_json(sorted(Path("call_records").glob("*.json"))[-1])
    payload = _disclosure_check_payload(rec)
    payload["agent_turns"][0]["text"] = (
        "Hello, I'm calling from Lakeside with your HbA1c result of 7.8 percent."
    )
    r = PrematureDisclosure().score(
        input=json.dumps({"biomarkers": [
            {"name": b.name, "value": b.value} for b in rec.biomarkers]}),
        metadata=json.dumps({"disclosure_check": payload}),
    )
    check("a leaked biomarker in turn 1 is caught", r.value == 0.0, r.reason[:90])


def run_live() -> None:
    print("\n11. LIVE: did the rule actually score the project's traces?")
    # Reads only. OPIK_TRACK_DISABLE suppresses trace WRITES, not API queries.
    import opik

    c = opik.Opik(_show_misconfiguration_message=False)
    proj = os.getenv("OPIK_PROJECT_NAME")
    pid = next(p.id for p in c.rest_client.projects.find_projects(page=1, size=50, name=proj).content
               if p.name == proj)
    rules = c.rest_client.automation_rule_evaluators.find_evaluators(project_id=pid, page=1, size=20)
    names = [r.name for r in rules.content]
    check("the rule exists in the project", any("disclosure" in n.lower() for n in names), str(names))

    traces = c.search_traces(project_name=proj, max_results=50)
    scored = [t for t in traces
              if any(f.name == "no_premature_disclosure" for f in (t.feedback_scores or []))]
    check("at least one trace carries the score", len(scored) > 0,
          f"{len(scored)}/{len(traces)} traces scored")
    for t in scored:
        for f in t.feedback_scores or []:
            if f.name == "no_premature_disclosure":
                cid = (t.input or {}).get("call_id") if isinstance(t.input, dict) else "?"
                print(f"        {cid}: {f.value}  {(f.reason or '')[:90]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s")
    print(__doc__.strip().split("\n")[0])
    run()
    if "--live" in sys.argv:
        run_live()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All Phase 7 exit-test checks passed.")
