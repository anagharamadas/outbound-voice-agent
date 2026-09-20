#!/usr/bin/env python3
"""Presenter view for the demo recording.

The worker's own log is JSON and full of framework internals -- correct for
production, unreadable on a screen recording. This renders the same events as
something a viewer can follow, and nothing else.

Two modes:

  LIVE -- pipe the worker through it. Prints only the beats that matter:
          session start, verification, handoff, tool calls, call end.

      ./venv/bin/python src/agent.py start 2>&1 | ./venv/bin/python demo_view.py

  POST-CALL -- render a finished call record: transcript, tool timeline, the
          deterministic/inferred split, discrepancies. This is the beat after
          you hang up.

      ./venv/bin/python demo_view.py --last          # most recent call
      ./venv/bin/python demo_view.py --watch         # wait for the next one
      ./venv/bin/python demo_view.py AJ_JzRRYmSEFUey # one specific call

Reads only. It never places a call, never writes, never touches Opik.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

RECORDS = Path(__file__).resolve().parent / "call_records"

# ANSI. Kept explicit rather than pulled from a library so this file has no
# dependencies and cannot break the demo by failing to import.
DIM, B, R = "\033[2m", "\033[1m", "\033[0m"
GREY, RED, GRN, YEL, BLU, CYN, MAG = (
    "\033[90m", "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[36m", "\033[35m",
)
W = 78


def rule(title: str = "", colour: str = CYN) -> str:
    if not title:
        return f"{colour}{'─' * W}{R}"
    pad = W - len(title) - 3
    return f"{colour}── {B}{title}{R}{colour} {'─' * max(pad, 0)}{R}"


def wrap(text: str, indent: int = 6, width: int = W) -> str:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width - indent:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(line)
    return f"\n{' ' * indent}".join(out)


# ─────────────────────────── live mode ───────────────────────────

# Only these reach the screen. Everything else is framework noise.
LIVE = [
    (r"starting session for patient (\S+)", GRN, "CALL STARTING", "patient {0}"),
    (r"observability: exporting to Opik", MAG, "OBSERVABILITY", "exporting to Opik"),
    (r"observability: OPIK_ENABLED is not set", GREY, "OBSERVABILITY", "no-op sink (Opik off)"),
    (r"call recording started: egress (\S+)", MAG, "RECORDING", "egress {0}"),
    (r"verification succeeded \((\S+)\) for patient (\S+)", GRN,
     "IDENTITY VERIFIED", "by {0} — handing off to VerifiedAgent"),
    (r"verification failed \((\S+)\), attempt (\d+) of (\d+)", RED,
     "VERIFICATION FAILED", "{0} — attempt {1} of {2}"),
    (r"verification: could not parse", YEL, "COULD NOT PARSE",
     "not a wrong answer — no attempt spent"),
    (r"call audio found: (\S+)", MAG, "AUDIO", "{0}"),
    (r"call record written to (\S+)", BLU, "RECORD WRITTEN", "{0}"),
    (r"call (\S+) recorded: (\d+) turn\(s\), (\d+) tool call\(s\), end_reason=(\S+)", BLU,
     "CALL ENDED", "{1} turns · {2} tool calls · {3}"),
    (r"call (\S+) analysed: booked=(\S+) verified=(\S+) outcome=(\S+)", GRN,
     "ANALYSIS COMPLETE", "booked={1} · verified={2} · {3}"),
    (r"OBSERVABILITY DATA LOST", RED, "TELEMETRY LOST", "a trace was dropped"),
    (r"post-call analysis exceeded", RED, "ANALYSIS TIMED OUT", "record is already safe"),
]


def live() -> None:
    print()
    print(rule("LIVE CALL", CYN))
    print()
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        # The worker logs JSON; console mode logs text. Handle both.
        msg = line
        if line.lstrip().startswith("{"):
            try:
                msg = json.loads(line).get("message", line)
            except json.JSONDecodeError:
                pass
        for pattern, colour, label, template in LIVE:
            m = re.search(pattern, msg)
            if m:
                stamp = datetime.now().strftime("%H:%M:%S")
                detail = template.format(*m.groups()) if m.groups() else template
                print(f"  {GREY}{stamp}{R}  {colour}{B}{label:<20}{R} {detail}")
                break
    print()
    print(rule("CALL OVER — run:  ./venv/bin/python demo_view.py --last", CYN))
    print()


# ──────────────────────── post-call mode ─────────────────────────

def latest() -> Path | None:
    files = list(RECORDS.glob("*.json"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def mask(number: str) -> str:
    return number if len(number) <= 7 else f"{number[:3]}{'*' * (len(number) - 7)}{number[-4:]}"


def render(path: Path) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    a = d.get("analysis") or {}
    det, inf = a.get("deterministic", {}), a.get("inferred") or {}
    disc = a.get("discrepancies") or []

    print()
    print(rule("CALL RECORD", CYN))
    print(f"  {B}{d['patient_name']}{R}  ·  {d['patient_id']}  ·  {d['duration_seconds']:.0f}s"
          f"  ·  ended: {d['end_reason']}")
    print(f"  {GREY}{path.name}{R}")

    print()
    print(rule("TRANSCRIPT", GREY))
    for t in d["transcript"]:
        who = f"{CYN}AGENT  {R}" if t["role"] == "assistant" else f"{YEL}PATIENT{R}"
        print(f"  {who} {wrap(t['text'])}")

    print()
    print(rule("TOOL CALLS  (the evidence, not the narration)", GREY))
    for t in d["tool_invocations"]:
        ok = f"{GRN}ok    {R}" if t["succeeded"] else f"{RED}FAILED{R}"
        when = t["at"][11:19]
        args = json.dumps(t["arguments"]) if t["arguments"] else "{}"
        print(f"  {GREY}{when}{R}  {ok} {B}{t['name']:<24}{R}")
        print(f"            {GREY}args  {args[:60]}{R}")
        print(f"            {GREY}→     {str(t['result'])[:60]}{R}")

    if not a:
        print()
        # Do not guess at the cause. The record simply has none.
        print(f"  {YEL}No analysis attached to this record.{R}")
        print(f"  {GREY}Either ANALYSIS_ENABLED=false, or the analysis had not finished")
        print(f"  when this was rendered — re-run with --last to see it.{R}")
        print()
        return

    print()
    print(rule("POST-CALL ANALYSIS", CYN))
    print(f"  {B}DETERMINISTIC{R} {GREY}— computed from the tool log. No model involved.{R}")
    for k in ("appointment_booked", "confirmation_id", "identity_verified",
              "verification_outcome", "verification_attempts", "tool_call_count"):
        if k in det:
            v = det[k]
            colour = GRN if v is True else (RED if v is False else "")
            print(f"     {k:<24} {colour}{v}{R}")

    print()
    print(f"  {B}INFERRED{R} {GREY}— a model reading the transcript. No computable ground truth.{R}")
    for k in ("outcome_category", "patient_sentiment", "biomarkers_communicated",
              "agent_gave_medical_advice", "objection_reason"):
        if k in inf:
            print(f"     {k:<24} {inf[k]}")
    if inf.get("medical_advice_evidence"):
        print(f"     {GREY}evidence: {wrap(inf['medical_advice_evidence'], 15)}{R}")
    if inf.get("summary"):
        print()
        print(f"     {wrap(inf['summary'], 5)}")

    print()
    if disc:
        print(rule("DISCREPANCIES — the model disagreed with the tool log", RED))
        for x in disc:
            print(f"  {RED}!{R} {wrap(x)}")
        print(f"  {GREY}The deterministic value wins. Both are kept.{R}")
    else:
        print(f"  {GRN}✓{R} No discrepancy — the model's reading matches the tool evidence.")

    print()
    print(rule("ARTIFACTS", GREY))
    audio = d.get("audio_path")
    egress = d.get("audio_egress_id")
    print(f"  record   {path}")
    if audio and Path(audio).is_file():
        print(f"  audio    {audio}  {GRN}(local, attached to the Opik trace){R}")
    elif egress:
        print(f"  audio    {GREY}remote — egress {egress}{R}")
    else:
        print(f"  audio    {GREY}not captured — console needs --record; a phone call")
        print(f"           needs CALL_RECORDING_ENABLED=true{R}")
    print()


# finish_call writes the record IMMEDIATELY, so a complete record survives
# whatever follows, and only then analyses the transcript and rewrites the file.
# So the first version of the file has no analysis in it. A fixed sleep is the
# wrong tool -- the analysis takes 2-6s plus two flushes, and a demo that
# renders too early reports "no analysis" for a call that analysed perfectly
# well. Poll for the real thing instead.
ANALYSIS_WAIT_SECONDS = 30


def wait_for_analysis(path: Path) -> bool:
    """Block until the record has an analysis, or until it clearly will not.

    Returns True if one arrived. Prints a progress line so a viewer watching a
    recording understands the pause is the system working rather than a hang.
    """
    deadline = time.time() + ANALYSIS_WAIT_SECONDS
    shown = False
    while time.time() < deadline:
        try:
            if (json.loads(path.read_text(encoding="utf-8")) or {}).get("analysis"):
                if shown:
                    print(f"\r  {GRN}analysis ready{R}{' ' * 40}")
                return True
        except (json.JSONDecodeError, OSError):
            pass                      # mid-rewrite; try again
        if not shown:
            print(f"  {GREY}call ended — running post-call analysis…{R}", end="", flush=True)
            shown = True
        time.sleep(0.4)
    if shown:
        print(f"\r  {YEL}no analysis after {ANALYSIS_WAIT_SECONDS}s{R}{' ' * 30}")
    return False


def main() -> int:
    if "--watch" in sys.argv:
        before = {f: f.stat().st_mtime for f in RECORDS.glob("*.json")} if RECORDS.exists() else {}
        print(f"\n  {GREY}waiting for the next call to finish… (Ctrl-C to stop){R}\n")
        while True:
            for f in RECORDS.glob("*.json"):
                if f not in before or f.stat().st_mtime > before.get(f, 0):
                    wait_for_analysis(f)
                    render(f)
                    return 0
            time.sleep(0.5)
    # An explicit record wins over --last. Needed to re-render an OLDER call --
    # re-recording one segment of a demo should not mean moving files around.
    named = [a for a in sys.argv[1:] if not a.startswith("--")]
    if named:
        path = Path(named[0])
        if not path.exists():
            path = RECORDS / named[0]
        if not path.exists() and not named[0].endswith(".json"):
            path = RECORDS / f"{named[0]}.json"
        if not path.exists():
            print(f"\n  {YEL}No record matching {named[0]!r}.{R}")
            print(f"  {GREY}Available: {', '.join(sorted(f.stem for f in RECORDS.glob('*.json')))}{R}\n")
            return 1
        render(path)
        return 0

    path = latest()
    if path is None:
        print(f"\n  {YEL}No call records yet.{R} Make a call first.\n")
        return 1
    render(path)
    return 0


if __name__ == "__main__":
    # Mode comes from the ARGUMENTS, not from isatty(). An earlier version
    # inferred it from the terminal and hung forever whenever stdin was a pipe
    # with nothing coming -- which is exactly how it would be run by accident
    # mid-demo.
    if "--last" in sys.argv or "--watch" in sys.argv or any(
        not a.startswith("--") for a in sys.argv[1:]
    ):
        raise SystemExit(main())
    if sys.stdin.isatty():
        print(__doc__)
        raise SystemExit(0)
    live()
