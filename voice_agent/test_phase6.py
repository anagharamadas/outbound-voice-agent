#!/usr/bin/env python3
"""Phase 6 exit test: the Opik integration module.

Most of this runs against a fake Opik client -- what matters structurally is
WHAT gets sent (one trace, a conversation span, a span per tool call, the
transcript out of metadata, the deterministic facts as tags), and none of that
needs the network to assert.

    ./venv/bin/python test_phase6.py          # structure + the seam, offline
    ./venv/bin/python test_phase6.py --live   # additionally export a real trace

--live sends the saved call record to Opik Cloud. The records are synthetic
(P001 is a fixture), but it IS a real export to a third party -- see the PII
note at the top of src/opik_integration.py.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DESTINATION_PHONE_NUMBER", "+10000000000")

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from src.events import CallRecord
from src.opik_integration import OpikSink, _audio_mime
from src.sinks import EmitResult, GuardedSink, build_sink

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


class FakeSpan:
    def __init__(self, **kw):
        self.kw = kw


class FakeTrace:
    def __init__(self, **kw):
        self.id = "fake-trace-id"
        self.kw = kw
        self.spans: list[FakeSpan] = []
        self.updates: list[dict] = []
        self.upserts = 0
        self.scores: list[dict] = []

    def span(self, **kw):
        s = FakeSpan(**kw)
        self.spans.append(s)
        return s

    def update(self, **kw):
        self.updates.append(kw)

    def log_feedback_score(self, **kw):
        self.scores.append(kw)


class FakeClient:
    def __init__(self, flush_returns=True):
        self.traces: list[FakeTrace] = []
        self.attachments: list[dict] = []
        self.flush_calls = 0
        self._flush_returns = flush_returns

    def trace(self, **kw):
        # Model the backend's upsert: a second call with the same id overwrites
        # rather than creating a second trace.
        tid = kw.get("id")
        if tid is not None:
            for existing in self.traces:
                if existing.id == tid:
                    existing.kw.update(kw)
                    existing.upserts += 1
                    return existing
        t = FakeTrace(**kw)
        self.traces.append(t)
        return t

    def queue_attachment_upload(self, **kw):
        self.attachments.append(kw)

    def flush(self, timeout=None):
        self.flush_calls += 1
        return self._flush_returns


def load_record() -> CallRecord | None:
    files = glob.glob("call_records/*.json")
    if not files:
        return None
    return CallRecord.read_json(Path(max(files, key=os.path.getmtime)))


def run(record: CallRecord) -> None:
    analysis = record.analysis or {}

    print("\n1. One trace per call, with the right spans")
    client = FakeClient()
    sink = OpikSink(client=client, project_name="test-project")
    result = sink.on_call_end(record)
    check("on_call_end delivered", result.delivered, result.detail)
    check("exactly one trace", len(client.traces) == 1, str(len(client.traces)))
    trace = client.traces[0]
    names = [s.kw.get("name") for s in trace.spans]
    check("a conversation span exists", "conversation" in names, str(names))
    expected_tools = [t.name for t in record.tool_invocations]
    check("one span per tool invocation",
          [n for n in names if n != "conversation"] == expected_tools, str(names))
    check("tool spans are typed 'tool'",
          all(s.kw.get("type") == "tool" for s in trace.spans if s.kw.get("name") != "conversation"))
    check("trace carries real historical timestamps",
          trace.kw.get("start_time") == record.started_at
          and trace.kw.get("end_time") == record.ended_at)

    print("\n2. The transcript goes in a SPAN, never in metadata")
    conv = next(s for s in trace.spans if s.kw.get("name") == "conversation")
    check("transcript is in span input", "transcript" in (conv.kw.get("input") or {}))
    blob = json.dumps(trace.kw.get("metadata") or {})
    check("trace metadata holds no transcript", "transcript" not in blob,
          "metadata is NOT truncated by Opik; a transcript there risks a 413")
    check("trace metadata stays small", len(blob) < 2000, f"{len(blob)} chars")

    print("\n3. Ids are never self-generated (UUIDv7 requirement)")
    check("no id passed to client.trace", trace.kw.get("id") is None)
    check("no id passed to any span", all(s.kw.get("id") is None for s in trace.spans))

    print("\n4. Deterministic facts become tags and scores")
    if analysis:
        r2 = sink.on_analysis(record, analysis)
        check("on_analysis delivered", r2.delivered, r2.detail)
        check("trace was updated, not duplicated", len(client.traces) == 1)
        check("analysis sent as a full upsert, not a partial update",
              trace.upserts == 1 and trace.updates == [],
              "Trace.update() races the batched create; the docs say re-send by id")
        tags = trace.kw.get("tags") or []
        det = analysis.get("deterministic", {})
        want = "booked" if det.get("appointment_booked") else "not-booked"
        check(f"tagged {want!r} from TOOL evidence", want in tags, str(tags))
        want_v = "verified" if det.get("identity_verified") else "unverified"
        check(f"tagged {want_v!r}", want_v in tags, str(tags))
        score_names = [s["name"] for s in trace.scores]
        check("booking logged as a numeric score", "appointment_booked" in score_names,
              str(score_names))
        check("verification logged as a numeric score", "identity_verified" in score_names)
    else:
        print("  SKIP  the saved record has no analysis")

    print("\n5. The audio is attached to the EXISTING trace")
    if record.audio_path and analysis:
        check("one attachment queued", len(client.attachments) == 1, str(len(client.attachments)))
        att = client.attachments[0]
        check("attached to the trace by id", att.get("entity_type") == "trace"
              and att.get("entity_id") == trace.id)
        check("correct mime for .ogg", _audio_mime("x.ogg") == "audio/vorbis")
        check("correct mime for .wav", _audio_mime("x.wav") == "audio/wav")
        check("unknown extension falls back", _audio_mime("x.zzz") == "application/octet-stream")
    else:
        print("  SKIP  the saved record has no audio_path")

    print("\n6. A dropped flush is reported, not swallowed")
    bad = OpikSink(client=FakeClient(flush_returns=False), project_name="t")
    r = bad.on_call_end(record)
    check("flush=False becomes EmitResult.failed", r.delivered is False, r.detail)
    check("detail explains why", "flush returned False" in r.detail, r.detail)

    print("\n7. on_analysis without on_call_end fails cleanly")
    orphan = OpikSink(client=FakeClient(), project_name="t")
    r = orphan.on_analysis(record, analysis or {})
    check("reported, not crashed", r.delivered is False, r.detail)

    print("\n8. A missing audio file is survivable")
    broken = record.with_audio_path("/nonexistent/path/audio.ogg")
    c = FakeClient()
    s2 = OpikSink(client=c, project_name="t")
    s2.on_call_end(broken)
    r = s2.on_analysis(broken, analysis or {})
    check("export still succeeds", r.delivered, r.detail)
    check("nothing attached", c.attachments == [])

    print("\n9. THE SEAM: disabling Opik changes the sink, not the agent")
    saved = os.environ.get("OPIK_ENABLED")
    try:
        os.environ["OPIK_ENABLED"] = "false"
        check("OPIK_ENABLED=false -> no-op sink", build_sink().name == "noop")
        os.environ["OPIK_ENABLED"] = "true"
        check("OPIK_ENABLED=true -> opik sink", build_sink().name == "opik")
    finally:
        if saved is None:
            os.environ.pop("OPIK_ENABLED", None)
        else:
            os.environ["OPIK_ENABLED"] = saved

    print("\n10. The agent does not import Opik")
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "opik", "src/agent.py", "src/events.py", "src/analysis.py",
         "src/booking.py", "src/patient.py", "src/prompts.py", "src/verification.py"],
        capture_output=True, text=True).stdout
    check("no Opik reference in any agent module", out.strip() == "",
          out.strip()[:120] or "clean")
    sinks_src = Path("src/sinks.py").read_text()
    top = sinks_src.split("def build_sink")[0]
    check("sinks.py has no module-level Opik import", "opik_integration" not in top,
          "the import is inside build_sink, so a deleted module degrades gracefully")


def run_live(record: CallRecord) -> None:
    print("\n11. LIVE: export a real trace to Opik Cloud")
    sink = GuardedSink(OpikSink())
    r1 = sink.on_call_end(record)
    check("trace exported", r1.delivered, r1.detail)
    if record.analysis:
        r2 = sink.on_analysis(record, record.analysis)
        check("analysis + audio exported", r2.delivered, r2.detail)
    print(f"        workspace : {os.getenv('OPIK_WORKSPACE')}")
    print(f"        project   : {os.getenv('OPIK_PROJECT_NAME')}")
    print(f"        call_id   : {record.call_id}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s")
    print(__doc__.strip().split("\n")[0])
    rec = load_record()
    if rec is None:
        print("\nNo saved call record in call_records/ -- run a console call first.")
        sys.exit(1)
    print(f"\nUsing {rec.call_id}: {len(rec.transcript)} turns, "
          f"{len(rec.tool_invocations)} tools, audio={'yes' if rec.audio_path else 'no'}, "
          f"analysis={'yes' if rec.analysis else 'no'}")
    run(rec)
    if "--live" in sys.argv:
        run_live(rec)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All Phase 6 exit-test checks passed.")
