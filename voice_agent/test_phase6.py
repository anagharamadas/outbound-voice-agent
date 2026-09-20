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
from test_fixtures import synthetic_record

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
    """The newest record that actually has a conversation in it.

    An empty record is a real thing -- a call that was answered and dropped
    before anyone spoke produces one -- but it exercises none of the structure
    under test here. Section 11 covers the empty case explicitly instead of
    letting it silently become the fixture for everything.
    """
    files = glob.glob("call_records/*.json")
    if not files:
        # Fresh clone: call_records/ is gitignored, so there is nothing to load.
        # Use a synthetic record rather than refusing to run -- a suite that
        # cannot run for a reviewer has told them nothing.
        return synthetic_record()
    records = sorted(
        (CallRecord.read_json(Path(f)) for f in files),
        key=lambda r: r.started_at,
    )
    with_content = [r for r in records if r.transcript]
    return (with_content or records)[-1]


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
    # NOT a magic number. What matters is that metadata is bounded BY
    # CONSTRUCTION, whatever the call looked like -- Opik does not truncate it,
    # so an unbounded field here is the 413 risk. The disclosure_check block is
    # capped at MAX_DISCLOSURE_TURNS x MAX_DISCLOSURE_TURN_CHARS, so the worst
    # case is ~23KB plus a small analysis. 64KB leaves room for both and still
    # catches an accidental transcript dump, which would run to megabytes.
    from src.opik_integration import MAX_DISCLOSURE_TURNS, MAX_DISCLOSURE_TURN_CHARS
    ceiling = MAX_DISCLOSURE_TURNS * MAX_DISCLOSURE_TURN_CHARS * 2 + 16_000
    check("trace metadata is bounded", len(blob) < ceiling,
          f"{len(blob)} chars, ceiling {ceiling}")

    # The property, tested against a call built to break it.
    from copy import deepcopy
    from src.events import TranscriptTurn
    from src.opik_integration import _disclosure_check_payload
    import dataclasses
    huge = dataclasses.replace(record, transcript=tuple(
        TranscriptTurn(role="assistant", text="x" * 5000, at=record.started_at)
        for _ in range(500)))
    payload = _disclosure_check_payload(huge)
    check("a 500-turn call with 5000-char turns is still capped",
          len(payload["agent_turns"]) <= MAX_DISCLOSURE_TURNS
          and all(len(t["text"]) <= MAX_DISCLOSURE_TURN_CHARS for t in payload["agent_turns"]),
          f"{len(payload['agent_turns'])} turns kept")
    check("that pathological call's metadata is still bounded",
          len(json.dumps(payload)) < ceiling,
          f"{len(json.dumps(payload))} chars")

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

    print("\n5b. A REMOTE recording is referenced, not attached")
    remote = record.with_audio_egress("EG_phone123").with_audio_path(
        "/livekit/egress/call-P001.ogg")   # a path on someone else's machine
    c = FakeClient()
    s3 = OpikSink(client=c, project_name="t")
    s3.on_call_end(remote)
    r = s3.on_analysis(remote, analysis or {})
    check("export succeeds", r.delivered, r.detail)
    check("nothing uploaded (the file is not here)", c.attachments == [])
    md = c.traces[0].kw.get("metadata") or {}
    check("the egress id is recorded", md.get("audio_egress_id") == "EG_phone123")
    check("the trace says the audio is NOT attached", md.get("audio_is_attached") is False,
          "so a reviewer knows to look at the egress rather than hunt for a player")

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

    print("\n10. An EMPTY call exports without crashing")
    import dataclasses
    empty = dataclasses.replace(record, transcript=(), tool_invocations=())
    c = FakeClient()
    s_empty = OpikSink(client=c, project_name="t")
    r = s_empty.on_call_end(empty)
    check("export succeeds", r.delivered, r.detail)
    check("one trace still created", len(c.traces) == 1)
    check("no conversation span, rather than an empty one",
          [sp.kw.get("name") for sp in c.traces[0].spans] == [],
          "a span with no turns would be noise in the UI")

    print("\n10b. Prewarm is an optimisation, never a dependency")
    import importlib
    import subprocess as _sp
    from src import sinks as _sinks

    saved_env = os.environ.get("OPIK_ENABLED")
    try:
        os.environ["OPIK_ENABLED"] = "false"
        before = dict(sys.modules)
        _sinks.prewarm()
        check("disabled -> prewarm imports nothing",
              set(sys.modules) - set(before) == set() or "opik" in before,
              "no point loading a sink that will not be used")

        # The claim under test: if the module is gone, prewarm must be a no-op,
        # not an exception. Simulated by making the import fail.
        os.environ["OPIK_ENABLED"] = "true"
        real = sys.modules.pop("src.opik_integration", None)
        sys.modules["src.opik_integration"] = None   # forces ImportError
        try:
            _sinks.prewarm()
            check("a broken/deleted sink module does not raise", True)
        except Exception as exc:
            check("a broken/deleted sink module does not raise", False, repr(exc))
        finally:
            if real is not None:
                sys.modules["src.opik_integration"] = real
            else:
                sys.modules.pop("src.opik_integration", None)

        # And the agent must still start with no Opik installed at all.
        probe = _sp.run(
            [sys.executable, "-c",
             "import sys, os;"
             "sys.path.insert(0,'.');"
             "os.environ['OPIK_ENABLED']='true';"
             # make `import opik` fail, as a deleted package would
             "sys.modules['opik']=None;"
             "from src.sinks import prewarm, build_sink;"
             "prewarm();"
             "print(build_sink().name)"],
            capture_output=True, text=True, cwd=".")
        check("with opik unimportable, the agent still builds a sink",
              probe.returncode == 0 and "noop" in probe.stdout,
              (probe.stdout + probe.stderr).strip().splitlines()[-1][:80] if (probe.stdout or probe.stderr) else "")
    finally:
        if saved_env is None:
            os.environ.pop("OPIK_ENABLED", None)
        else:
            os.environ["OPIK_ENABLED"] = saved_env

    print("\n11. The agent does not import Opik")
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "opik", "src/agent.py", "src/events.py", "src/analysis.py",
         "src/booking.py", "src/patient.py", "src/prompts.py", "src/verification.py"],
        capture_output=True, text=True).stdout
    check("no Opik reference in any agent module", out.strip() == "",
          out.strip()[:120] or "clean")
    # Asserted on the AST, not on text position. The property is "nothing at
    # MODULE scope imports the Opik adapter" -- which function holds the import
    # is irrelevant, and a text search anchored to one function name breaks the
    # moment another function is added above it.
    import ast as _ast
    module_body = _ast.parse(Path("src/sinks.py").read_text()).body
    top_level_imports = {
        _ast.unparse(n) for n in module_body
        if isinstance(n, (_ast.Import, _ast.ImportFrom))
    }
    check("sinks.py has no module-level Opik import",
          not any("opik" in i.lower() for i in top_level_imports),
          "every Opik import is function-scoped, so a deleted module degrades gracefully")
    check("sinks.py has no module-level import of the adapter at all",
          not any("opik_integration" in i for i in top_level_imports),
          str(sorted(top_level_imports))[:90])


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
