"""The Opik observability sink. The ONLY file in this repo that imports Opik.

This is the whole of D5's claim, in one place. `src/agent.py` does not import
Opik, does not name an Opik symbol, and does not know this file exists; it emits
a finished `CallRecord` to whatever `build_sink()` hands back. Delete this file
and remove the one branch in `src/sinks.py` and the agent still runs, still
records calls, still analyses them -- it just stops exporting.

NOTE ON PII -- read before running this against a real patient.
    This module sends a patient's name, phone number, date-of-birth-verified
    status, biomarker names and values, and the full transcript of a healthcare
    conversation to a third-party SaaS platform. That is acceptable for a demo
    against synthetic records. It is NOT acceptable for production without
    either (a) redacting identifiers and health values before they leave the
    process, or (b) running a self-hosted Opik deployment inside the same trust
    boundary as the patient data. Opik supports self-hosting; see
    docs/recon-opik.md section 4. The phone number in particular is logged in
    full below because the brief asks for call metadata -- in production it
    would be masked to the last four digits, as src/config.py already does for
    its own logging.

API surface verified against the INSTALLED opik 2.2.71, not from the docs or
from memory:
  - Opik(project_name=, workspace=, host=, api_key=) -- env/`~/.opik.config`
    supply all four when omitted
  - Opik.trace(...) -> Trace; Trace.span(...) -> Span; both take start_time and
    end_time, which is what lets a finished record be replayed with its real
    historical timestamps rather than the moment of export
  - Opik.flush(timeout: int|None) -> bool  (True only on complete delivery)
  - Opik.queue_attachment_upload(entity_type, entity_id, project_name,
    file_path, file_name=, mime_type=) -- non-blocking; flush() waits for it
  - Trace.update(...), Trace.log_feedback_score(name, value, ...)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import opik

try:
    from .events import CallRecord
    from .sinks import EmitResult
except ImportError:  # running as a script, see the note in src/agent.py
    from src.events import CallRecord
    from src.sinks import EmitResult

logger = logging.getLogger("healthcare-agent.opik")

# Flush is the only blocking call here. Capped so a slow or unreachable Opik
# cannot eat the shutdown budget -- see SHUTDOWN_TIMEOUT_SECONDS in agent.py for
# how the whole budget is apportioned.
#
# Two different budgets, because they are two different kinds of work. The trace
# is a few KB of JSON. The analysis flush also waits for the audio upload, and a
# real 2.5MB Ogg did NOT finish in 3s on a first attempt -- the flush returned
# False with "Still uploading 1 file(s)". A too-short timeout there reports data
# loss that has not happened, which is worse than useless in a signal whose
# whole job is to be trusted.
FLUSH_TIMEOUT_SECONDS = 3
ATTACHMENT_FLUSH_TIMEOUT_SECONDS = 15

# Opik previews these; `lk agent console --record` writes Ogg/Opus, and a real
# call's egress may write WAV. Anything else is still uploaded, just without an
# in-browser player.
AUDIO_MIME_TYPES = {".ogg": "audio/vorbis", ".wav": "audio/wav"}


# The online evaluation rule (Phase 7) is a trace-scope Python metric, and a
# trace rule can only read the trace's ROOT objects -- input, output, metadata.
# Verified empirically against a probe rule: the transcript lives in a span and
# is simply not reachable from there. So the raw material the rule needs is
# mirrored into metadata in bounded form, while the full transcript stays in its
# span for humans reading the trace. Two audiences, two shapes.
#
# What is mirrored is deliberately NOT the answer. The sink supplies an ordering
# fact that is cheap and structural -- which agent turns preceded a successful
# verification -- and the rule does the semantic work of deciding whether any of
# them disclosed a biomarker. Precomputing the verdict here would leave the rule
# echoing rather than evaluating.
MAX_DISCLOSURE_TURNS = 60
MAX_DISCLOSURE_TURN_CHARS = 400

VERIFY_TOOL = "verify_patient_identity"
VERIFIED_RESULT = "verified"


def _disclosure_check_payload(record: CallRecord) -> dict[str, Any]:
    """Agent turns, each flagged with whether it preceded verification.

    Ordering is decided at TURN granularity, not by comparing raw timestamps.
    Measured on a real call, the first biomarker-bearing turn is stamped 7ms
    after the verification tool returned -- because LiveKit stamps a message
    when its turn BEGINS, not when it is delivered. Adjacent turns are a median
    of 11.7s apart, so a turn-level comparison has seconds of slack where a
    microsecond one has none.
    """
    verified_at = None
    for call in record.tool_invocations:
        if call.name == VERIFY_TOOL and call.result == VERIFIED_RESULT:
            verified_at = call.at
            break

    turns = []
    for turn in record.transcript:
        if turn.role != "assistant":
            continue
        turns.append(
            {
                "text": turn.text[:MAX_DISCLOSURE_TURN_CHARS],
                # None verification at all means every turn precedes it, which
                # is the correct reading: nothing was ever verified.
                "before_verification": verified_at is None or turn.at < verified_at,
            }
        )
        if len(turns) >= MAX_DISCLOSURE_TURNS:
            break

    return {"identity_verified": verified_at is not None, "agent_turns": turns}


def _audio_mime(path: str) -> str:
    for suffix, mime in AUDIO_MIME_TYPES.items():
        if path.lower().endswith(suffix):
            return mime
    return "application/octet-stream"


class OpikSink:
    """One trace per call, built from a finished record.

    Nothing here is instrumented live. The record arrives complete and is
    replayed into Opik in one burst with its original timestamps -- which the
    recon confirmed is supported, and which is the only reason the agent can
    stay ignorant of this file.
    """

    name = "opik"

    def __init__(self, client: Any | None = None, project_name: str | None = None) -> None:
        # Project name from the environment, matching every other Opik setting.
        # Constructing the client reads OPIK_API_KEY / OPIK_WORKSPACE /
        # OPIK_URL_OVERRIDE itself; passing them explicitly would just be a
        # second place for them to go stale.
        self._project_name = project_name or (os.getenv("OPIK_PROJECT_NAME") or "").strip() or None
        self._client = client if client is not None else opik.Opik(project_name=self._project_name)
        # Remembered so on_analysis can update the trace on_call_end created,
        # rather than opening a second one for the same call.
        self._traces: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    def on_call_start(self, record_id: str) -> None:
        """Deliberately does nothing.

        The trace is created at the END of the call, from the finished record.
        Opening it here would mean holding a half-built trace across the call
        and hoping the process survives to close it -- the failure mode D5 was
        written to avoid.
        """
        logger.debug("opik sink: call %s started (trace is created at call end)", record_id)

    def on_call_end(self, record: CallRecord) -> EmitResult:
        try:
            trace = self._build_trace(record)
        except Exception as exc:
            # GuardedSink would catch this anyway; catching here lets the reason
            # travel back in the EmitResult instead of only into the log.
            logger.exception("opik: failed to build the trace for call %s", record.call_id)
            return EmitResult.failed(f"trace build failed: {type(exc).__name__}: {exc}")

        self._traces[record.call_id] = trace
        return self._flush(f"trace for call {record.call_id}")

    def on_analysis(self, record: CallRecord, analysis: dict[str, Any]) -> EmitResult:
        """Attach the analysis, and the recording, to the trace already created.

        The audio arrives here rather than in `on_call_end` because the console
        host is still writing it when the call ends -- see the ordering note in
        `agent.finish_call`. `queue_attachment_upload` takes an entity id, so
        attaching to an existing trace is a first-class operation, not a
        workaround.
        """
        trace = self._traces.get(record.call_id)
        if trace is None:
            return EmitResult.failed(
                f"no trace held for call {record.call_id}; on_call_end did not run or failed"
            )

        try:
            deterministic = analysis.get("deterministic", {}) or {}
            inferred = analysis.get("inferred") or {}
            discrepancies = analysis.get("discrepancies") or []

            # Re-send the FULL payload under the same id rather than calling
            # trace.update(). The backend upserts, so this overwrites cleanly --
            # and it sidesteps a documented race the SDK warns about out loud:
            # "Calling Trace.update() shortly after creation with batching
            # enabled may cause data loss", because an update can overtake the
            # batched create and be dropped against a trace the server has not
            # seen yet. Reusing Opik's own id is not the same as generating one;
            # the UUIDv7 rule is still intact.
            self._client.trace(
                id=trace.id,
                **self._trace_payload(record, analysis=analysis),
            )

            # Numeric so the dashboard can aggregate them across calls. These are
            # OUR scores, computed from tool evidence; Phase 7's online rule adds
            # its own alongside.
            for name, value in (
                ("appointment_booked", deterministic.get("appointment_booked")),
                ("identity_verified", deterministic.get("identity_verified")),
                ("agent_gave_medical_advice", inferred.get("agent_gave_medical_advice")),
            ):
                if isinstance(value, bool):
                    trace.log_feedback_score(name=name, value=1.0 if value else 0.0)

            if discrepancies:
                trace.log_feedback_score(
                    name="analysis_discrepancy",
                    value=1.0,
                    reason="; ".join(discrepancies)[:1000],
                )

            self._attach_audio(record, trace)
        except Exception as exc:
            logger.exception("opik: failed to attach analysis for call %s", record.call_id)
            return EmitResult.failed(f"analysis attach failed: {type(exc).__name__}: {exc}")

        # The longer budget: this flush also waits for the audio upload.
        return self._flush(
            f"analysis for call {record.call_id}",
            timeout=ATTACHMENT_FLUSH_TIMEOUT_SECONDS,
        )

    # -- construction ------------------------------------------------------

    def _trace_payload(
        self, record: CallRecord, *, analysis: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The complete trace payload, with or without the analysis.

        Built in one place because it is sent TWICE -- once when the call ends,
        once when the analysis is ready -- and the second send must be complete,
        not a delta. See the upsert note in `on_analysis`.
        """
        deterministic = (analysis or {}).get("deterministic", {}) or {}
        inferred = (analysis or {}).get("inferred") or {}
        discrepancies = (analysis or {}).get("discrepancies") or []

        output: dict[str, Any] = {
            "end_reason": record.end_reason,
            "duration_seconds": record.duration_seconds,
        }
        metadata: dict[str, Any] = {
            "end_reason": record.end_reason,
            "duration_seconds": record.duration_seconds,
            "turn_count": len(record.transcript),
            "tool_call_count": len(record.tool_invocations),
            "audio_path": record.audio_path,
            # Present whether or not a file could be attached, so a reviewer
            # looking at a phone call's trace can still find the recording.
            "audio_egress_id": record.audio_egress_id,
            "audio_is_attached": bool(
                record.audio_path and os.path.isfile(record.audio_path)
            ),
            # Present on BOTH sends. It does not depend on the analysis, and the
            # online rule fires on the create as well as the upsert -- a rule
            # that saw it only on the second pass would score half the traces
            # against missing data.
            "disclosure_check": _disclosure_check_payload(record),
        }
        if analysis:
            output.update(
                {
                    "outcome": inferred.get("outcome_category"),
                    "summary": inferred.get("summary"),
                    "appointment_booked": deterministic.get("appointment_booked"),
                    "confirmation_id": deterministic.get("confirmation_id"),
                    "discrepancies": discrepancies,
                }
            )
            # Small and filterable only. The transcript stays in its span --
            # metadata is NOT truncated and counts toward the request cap, so a
            # long transcript here risks a 413 on ingestion.
            metadata["analysis"] = analysis

        return {
            "name": f"call {record.patient_id} {record.started_at:%Y-%m-%d %H:%M}",
            "start_time": record.started_at,
            "end_time": record.ended_at,
            "input": {
                "patient_id": record.patient_id,
                "patient_name": record.patient_name,
                # NOTE (PII): full number, see the module docstring. Production
                # would mask this.
                "biomarkers": [
                    {"name": b.name, "value": b.value, "unit": b.unit, "status": b.status}
                    for b in record.biomarkers
                ],
                "room_name": record.room_name,
                "call_id": record.call_id,
            },
            "output": output,
            "metadata": metadata,
            "tags": self._tags(record, deterministic, inferred, discrepancies),
            "project_name": self._project_name,
        }

    def _build_trace(self, record: CallRecord) -> Any:
        """One trace, with a conversation span and one span per tool call.

        Ids are never self-generated. Opik requires UUIDv7 and an ordinary
        uuid4() fails at ingestion, far from the call site -- so the client
        makes the trace and the trace makes its own spans.
        """
        trace = self._client.trace(**self._trace_payload(record))

        if record.transcript:
            trace.span(
                name="conversation",
                type="general",
                start_time=record.transcript[0].at,
                end_time=record.transcript[-1].at,
                # Transcript belongs HERE, in span input, where oversized content
                # is truncated rather than rejected.
                input={
                    "transcript": [
                        {"role": t.role, "text": t.text, "at": t.at.isoformat()}
                        for t in record.transcript
                    ]
                },
                output={"final_agent_message": self._last_agent_text(record)},
                metadata={"turn_count": len(record.transcript)},
            )

        for call in record.tool_invocations:
            trace.span(
                name=call.name,
                type="tool",
                # A tool call is recorded as an instant, not an interval -- the
                # record carries one timestamp per invocation. Equal start and
                # end is honest about that; inventing a duration would not be.
                start_time=call.at,
                end_time=call.at,
                input=dict(call.arguments),
                output={"result": call.result, "succeeded": call.succeeded},
                tags=["tool-ok"] if call.succeeded else ["tool-failed"],
            )

        return trace

    def _attach_audio(self, record: CallRecord, trace: Any) -> None:
        if not record.audio_path and not record.audio_egress_id:
            logger.debug("opik: no audio to attach for call %s", record.call_id)
            return

        path = record.audio_path
        if not path or not os.path.isfile(path):
            # A phone call. The recording exists, but not on this machine -- it
            # was written by LiveKit egress to wherever that egress was pointed.
            # Recording the reference is the documented fallback and is what the
            # brief permits; it is NOT a warning, because nothing went wrong.
            logger.info(
                "opik: call %s audio is remote (egress=%s, location=%s); "
                "recording the reference rather than attaching a file",
                record.call_id,
                record.audio_egress_id,
                path,
            )
            return
        self._client.queue_attachment_upload(
            entity_type="trace",
            entity_id=trace.id,
            project_name=self._project_name or "Default Project",
            file_path=path,
            file_name=os.path.basename(path),
            mime_type=_audio_mime(path),
        )
        logger.info("opik: queued audio attachment %s for call %s", path, record.call_id)

    @staticmethod
    def _last_agent_text(record: CallRecord) -> str | None:
        for turn in reversed(record.transcript):
            if turn.role == "assistant":
                return turn.text
        return None

    @staticmethod
    def _tags(
        record: CallRecord,
        deterministic: dict[str, Any],
        inferred: dict[str, Any],
        discrepancies: list[Any],
    ) -> list[str]:
        """Filterable in the UI without opening a trace.

        Deterministic facts first: these are the ones that are true regardless
        of what any model said about the call.
        """
        tags = ["healthcare-voice-agent"]
        if record.end_reason:
            tags.append(f"end:{record.end_reason}")
        if deterministic:
            tags.append(
                "verified" if deterministic.get("identity_verified") else "unverified"
            )
            tags.append("booked" if deterministic.get("appointment_booked") else "not-booked")
        if inferred.get("outcome_category"):
            tags.append(f"outcome:{inferred['outcome_category']}")
        if inferred.get("agent_gave_medical_advice"):
            tags.append("advice-flagged")
        if discrepancies:
            tags.append("discrepancy")
        return tags

    # -- delivery ----------------------------------------------------------

    def _flush(self, what: str, timeout: int = FLUSH_TIMEOUT_SECONDS) -> EmitResult:
        """Opik logs from a BACKGROUND THREAD and reports a dropped message by
        returning False, not by raising. An unchecked flush is therefore a
        silently empty project: the call sounds perfect, the process exits
        cleanly, and nothing recorded that anything was lost.
        """
        delivered = self._client.flush(timeout=timeout)
        if not delivered:
            return EmitResult.failed(
                f"opik flush returned False within {timeout}s "
                f"({what}); some data was dropped or timed out"
            )
        logger.debug("opik: delivered %s", what)
        return EmitResult.ok(what)
