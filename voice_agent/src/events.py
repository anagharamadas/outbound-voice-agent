"""What one call produced, as data.

This module is the boundary between the call and everything downstream of it.
Nothing here knows about LiveKit, Opik, or any other platform: a `CallRecord`
is a plain value that could have come from a phone call, a replayed fixture or
a test. That is what makes the Phase 6 observability sink a pure function of a
finished record rather than a set of hooks threaded through the call path (D5).

The `CallRecorder` accumulates events while the call runs and freezes them into
a `CallRecord` at the end. It is deliberately dumb -- it appends what it is
given and computes nothing -- because anything clever here would be a second
place where call outcome is decided, and D4 puts that in one place (Phase 5).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .patient import Biomarker, Patient
    from .verification import VerificationAttempt
except ImportError:  # running as a script, see the note in src/agent.py
    from src.patient import Biomarker, Patient
    from src.verification import VerificationAttempt


def utcnow() -> datetime:
    """Timezone-aware UTC. Naive datetimes are a liability once these timestamps
    are compared against a platform's own clock in Phase 6/7."""
    return datetime.now(timezone.utc)


def from_epoch(seconds: float) -> datetime:
    """LiveKit stamps its events with `time.time()` floats (verified on
    `ChatMessage.created_at` and `CloseEvent.created_at` in the installed
    1.8.2). Convert at the boundary so nothing downstream handles two
    representations of the same thing."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@dataclass(frozen=True)
class TranscriptTurn:
    """One conversational turn.

    `role` carries LiveKit's own vocabulary ('user' | 'assistant'), not a
    remapped one, so a turn can be matched back to the session history it came
    from without a translation table.
    """

    role: str
    text: str
    at: datetime
    interrupted: bool = False


@dataclass(frozen=True)
class ToolInvocation:
    """One tool call and what it returned.

    This is the Phase 4 home of what Phase 3 kept as `booking.ToolCallRecord`.
    It gains a timestamp, which the Phase 3 version had no use for and the
    premature-disclosure evaluation (Phase 7) cannot work without: that check is
    a comparison between when verification succeeded and when a biomarker was
    first spoken, and neither side of it is answerable without a clock.
    """

    name: str
    arguments: dict[str, Any]
    result: str
    at: datetime
    succeeded: bool


@dataclass(frozen=True)
class CallRecord:
    """Everything one call produced. Immutable once built.

    `analysis` is the slot Phase 5 fills. It is left as an open mapping rather
    than a typed result because Phase 5 has not defined that shape yet, and
    guessing it here would be a contract two phases would then have to agree on
    by luck. Phase 5 replaces this with its own type.

    Frozen, so a sink cannot quietly mutate the record it was handed and change
    what a later sink sees. Use `dataclasses.replace` to attach the analysis.
    """

    call_id: str
    room_name: str
    patient_id: str
    patient_name: str
    biomarkers: tuple[Biomarker, ...]
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    end_reason: str | None = None
    transcript: tuple[TranscriptTurn, ...] = ()
    tool_invocations: tuple[ToolInvocation, ...] = ()
    verification_attempts: tuple[VerificationAttempt, ...] = ()
    # Phase 6 attaches the real .wav; the recon confirmed audio/wav is a
    # supported attachment type, so a path here is preferred to a bare URI.
    audio_path: str | None = None
    analysis: dict[str, Any] | None = None

    def with_analysis(self, analysis: dict[str, Any]) -> CallRecord:
        """Phase 5's entry point. Returns a new record; does not mutate."""
        return replace(self, analysis=analysis)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, for the local inspection file and for Phase 6.

        Datetimes become ISO 8601 strings. This is the only place that decides
        the serialised shape, so a sink never invents its own.
        """

        def turn(t: TranscriptTurn) -> dict[str, Any]:
            return {
                "role": t.role,
                "text": t.text,
                "at": t.at.isoformat(),
                "interrupted": t.interrupted,
            }

        def tool(t: ToolInvocation) -> dict[str, Any]:
            return {
                "name": t.name,
                "arguments": t.arguments,
                "result": t.result,
                "at": t.at.isoformat(),
                "succeeded": t.succeeded,
            }

        return {
            "call_id": self.call_id,
            "room_name": self.room_name,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "biomarkers": [asdict(b) for b in self.biomarkers],
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "end_reason": self.end_reason,
            "transcript": [turn(t) for t in self.transcript],
            "tool_invocations": [tool(t) for t in self.tool_invocations],
            "verification_attempts": [asdict(v) for v in self.verification_attempts],
            "audio_path": self.audio_path,
            "analysis": self.analysis,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallRecord:
        """Rebuild a record from `to_dict` output. The inverse of it.

        Phase 5 needs this: its exit test analyses a record saved by Phase 4,
        and re-running a call to get one back would make the analysis untestable
        without a phone. Round-tripping through the JSON also keeps the two
        halves honest -- a field added to one and forgotten in the other shows
        up immediately.
        """

        def dt(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        started = dt(data["started_at"])
        assert started is not None, "started_at is required"

        return cls(
            call_id=data["call_id"],
            room_name=data["room_name"],
            patient_id=data["patient_id"],
            patient_name=data["patient_name"],
            biomarkers=tuple(Biomarker(**b) for b in data.get("biomarkers", ())),
            started_at=started,
            ended_at=dt(data.get("ended_at")),
            duration_seconds=data.get("duration_seconds"),
            end_reason=data.get("end_reason"),
            transcript=tuple(
                TranscriptTurn(
                    role=t["role"],
                    text=t["text"],
                    at=datetime.fromisoformat(t["at"]),
                    interrupted=t.get("interrupted", False),
                )
                for t in data.get("transcript", ())
            ),
            tool_invocations=tuple(
                ToolInvocation(
                    name=t["name"],
                    arguments=t.get("arguments", {}),
                    result=t["result"],
                    at=datetime.fromisoformat(t["at"]),
                    succeeded=t["succeeded"],
                )
                for t in data.get("tool_invocations", ())
            ),
            verification_attempts=tuple(
                VerificationAttempt(**v) for v in data.get("verification_attempts", ())
            ),
            audio_path=data.get("audio_path"),
            analysis=data.get("analysis"),
        )

    @classmethod
    def read_json(cls, path: Path) -> CallRecord:
        """Load a record written by `write_json`."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write_json(self, path: Path) -> Path:
        """Write the record where a human can read it.

        This is NOT the observability sink and must not be confused with one.
        It exists so the exit test can inspect a complete record with the sink
        disabled entirely -- which is the whole claim D5 makes, and it is worth
        being able to demonstrate rather than assert.

        The file contains biomarkers and a full transcript. It is patient health
        data and the directory is gitignored; do not move it somewhere tracked.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path


class CallRecorder:
    """Accumulates call events, then freezes them.

    One instance per call. Not thread-safe and does not need to be: LiveKit
    dispatches session events on the event loop, so every `add_*` here runs on
    the same thread.
    """

    def __init__(self, *, call_id: str, room_name: str, patient: Patient) -> None:
        self.call_id = call_id
        self.room_name = room_name
        self._patient = patient
        self._started_at = utcnow()
        self._ended_at: datetime | None = None
        self._end_reason: str | None = None
        self._turns: list[TranscriptTurn] = []
        self._tools: list[ToolInvocation] = []
        self._audio_path: str | None = None

    @property
    def started_at(self) -> datetime:
        return self._started_at

    def add_turn(
        self, *, role: str, text: str, at: datetime | None = None, interrupted: bool = False
    ) -> None:
        self._turns.append(
            TranscriptTurn(role=role, text=text, at=at or utcnow(), interrupted=interrupted)
        )

    def add_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: str,
        succeeded: bool,
        at: datetime | None = None,
    ) -> None:
        self._tools.append(
            ToolInvocation(
                name=name,
                arguments=arguments,
                result=result,
                at=at or utcnow(),
                succeeded=succeeded,
            )
        )

    def set_audio_path(self, path: str | None) -> None:
        self._audio_path = path

    def note_end(self, *, reason: str, at: datetime | None = None) -> None:
        """Record why the call ended.

        First writer wins. The close event and the shutdown callback can both
        fire, and the first carries the real reason ('user_initiated',
        'participant_disconnected') while the second reports only that the job
        is going away -- so a later, vaguer reason must not overwrite it.
        """
        if self._end_reason is not None:
            return
        self._end_reason = reason
        self._ended_at = at or utcnow()

    def build(
        self,
        *,
        verification_attempts: tuple[VerificationAttempt, ...] = (),
        tool_invocations: tuple[ToolInvocation, ...] = (),
    ) -> CallRecord:
        """Freeze into a `CallRecord`.

        `verification_attempts` and `tool_invocations` are passed in rather than
        accumulated here because both live on the agent instances, and the
        verified agent does not exist until a handoff that may never happen.
        The caller reaches them at the end, when it is known what exists.
        """
        ended_at = self._ended_at or utcnow()
        return CallRecord(
            call_id=self.call_id,
            room_name=self.room_name,
            patient_id=self._patient.patient_id,
            patient_name=self._patient.identity.name,
            biomarkers=self._patient.health.biomarkers,
            started_at=self._started_at,
            ended_at=ended_at,
            duration_seconds=round((ended_at - self._started_at).total_seconds(), 3),
            end_reason=self._end_reason,
            transcript=tuple(self._turns),
            tool_invocations=tuple(self._tools) + tuple(tool_invocations),
            verification_attempts=tuple(verification_attempts),
            audio_path=self._audio_path,
        )
