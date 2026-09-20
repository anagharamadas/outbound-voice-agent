"""The observability seam.

The agent emits a finished `CallRecord` to whatever sink is configured and does
not know, or care, what that sink does with it. Opik arrives in Phase 6 as one
implementation of `ObservabilitySink` and nothing in `src/agent.py` changes to
accommodate it. Deleting the Opik module must leave a working agent -- that is
the requirement this file exists to satisfy (D5).

Two properties are load-bearing and easy to lose:

1. **A sink failure must never reach the call path.** Observability is not worth
   dropping a call to a patient. `GuardedSink` enforces this by wrapping every
   delegate, so the guarantee lives in one place rather than in each sink's
   good intentions.

2. **A sink must be able to say it failed.** `on_call_end` returns an
   `EmitResult`, never `None`. Phase 6 needs this: Opik logs from a background
   thread and its `flush()` returns False on a dropped message rather than
   raising, so a sink that cannot report failure would turn the single
   highest-severity silent failure in the build into one with no symptom at all
   -- the call sounds perfect, the process exits cleanly, and the trace is
   simply absent. Fixing the return type later would mean editing an
   already-wired seam, which is the rework this plan is written to avoid.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

try:
    from .events import CallRecord
except ImportError:  # running as a script, see the note in src/agent.py
    from src.events import CallRecord

logger = logging.getLogger("healthcare-agent.sinks")


@dataclass(frozen=True)
class EmitResult:
    """Did the record actually get where it was going?

    `detail` carries the reason on failure. It is free text for a human reading
    logs, not a closed set to branch on -- the caller's only decision is whether
    to shout, and `delivered` answers that.
    """

    delivered: bool
    detail: str = ""

    @classmethod
    def ok(cls, detail: str = "") -> EmitResult:
        return cls(delivered=True, detail=detail)

    @classmethod
    def failed(cls, detail: str) -> EmitResult:
        return cls(delivered=False, detail=detail)


@runtime_checkable
class ObservabilitySink(Protocol):
    """Three methods, deliberately.

    A wide interface would defeat the purpose: every method here is one the
    agent must call at the right moment, and each addition is another thing a
    future sink has to implement correctly to avoid silently doing nothing.
    """

    def on_call_start(self, record_id: str) -> None:
        """Called once, as the call begins. Takes only an id: nothing useful is
        known yet, and passing a half-built record invites a sink to emit it."""

    def on_call_end(self, record: CallRecord) -> EmitResult:
        """Called once, with the finished record. MUST return an EmitResult."""

    def on_analysis(self, record: CallRecord, analysis: dict[str, Any]) -> EmitResult:
        """Called after Phase 5's post-call analysis. MUST return an EmitResult."""


class NoOpSink:
    """The default. Does nothing, successfully.

    Returning `ok` rather than `failed` is correct: the no-op sink delivered
    everything it was asked to deliver, which was nothing. Reporting failure
    here would train whoever reads the logs to ignore a real one.
    """

    name = "noop"

    def on_call_start(self, record_id: str) -> None:
        logger.debug("noop sink: call %s started", record_id)

    def on_call_end(self, record: CallRecord) -> EmitResult:
        logger.debug("noop sink: call %s ended", record.call_id)
        return EmitResult.ok("no-op sink")

    def on_analysis(self, record: CallRecord, analysis: dict[str, Any]) -> EmitResult:
        logger.debug("noop sink: analysis for call %s", record.call_id)
        return EmitResult.ok("no-op sink")


class GuardedSink:
    """Wraps a sink so its failures are logged loudly and go no further.

    "Loudly" and "swallowed" are not in tension. The call continues -- the
    person on the phone is unaffected by a telemetry problem -- and the operator
    still learns the record was lost, with a stack trace naming the layer at
    fault. A bare `except: pass` here would satisfy the first half and betray
    the second (hard rule 6).
    """

    def __init__(self, delegate: ObservabilitySink) -> None:
        self._delegate = delegate

    @property
    def name(self) -> str:
        return getattr(self._delegate, "name", type(self._delegate).__name__)

    def on_call_start(self, record_id: str) -> None:
        try:
            self._delegate.on_call_start(record_id)
        except Exception:
            logger.exception("observability sink %r failed on_call_start", self.name)

    def on_call_end(self, record: CallRecord) -> EmitResult:
        try:
            result = self._delegate.on_call_end(record)
        except Exception as exc:
            logger.exception("observability sink %r raised on_call_end", self.name)
            return EmitResult.failed(f"{type(exc).__name__}: {exc}")
        return self._checked(result, "on_call_end", record.call_id)

    def on_analysis(self, record: CallRecord, analysis: dict[str, Any]) -> EmitResult:
        try:
            result = self._delegate.on_analysis(record, analysis)
        except Exception as exc:
            logger.exception("observability sink %r raised on_analysis", self.name)
            return EmitResult.failed(f"{type(exc).__name__}: {exc}")
        return self._checked(result, "on_analysis", record.call_id)

    def _checked(self, result: Any, method: str, call_id: str) -> EmitResult:
        """A sink that returns None is a bug, and a quiet one -- the caller sees
        a falsy value and reasonably reads it as failure. Say so explicitly."""
        if not isinstance(result, EmitResult):
            logger.error(
                "observability sink %r returned %r from %s, expected EmitResult",
                self.name,
                type(result).__name__,
                method,
            )
            return EmitResult.failed(f"{self.name}.{method} did not return an EmitResult")
        if not result.delivered:
            # The loud part. Nothing else in the system will notice this.
            logger.error(
                "OBSERVABILITY DATA LOST: sink %r did not deliver call %s (%s): %s",
                self.name,
                call_id,
                method,
                result.detail or "no detail given",
            )
        return result


def opik_enabled() -> bool:
    """One place decides. Both `prewarm` and `build_sink` ask it."""
    return (os.getenv("OPIK_ENABLED") or "").strip().lower() in ("1", "true", "yes")


def prewarm() -> None:
    """Load whatever the configured sink needs, BEFORE any call arrives.

    Pure optimisation. It changes nothing about what the agent does and may fail
    freely -- `build_sink()` does its own import later and does not care whether
    this ran.

    WHY IT EXISTS. Importing `opik` takes ~888ms: it pulls in sentry_sdk and a
    good deal else. That import otherwise happens inside `build_sink()` at
    session start, on the agent's event loop, at the exact moment the opening
    line should be going out -- measured on a real call, and flagged by the
    framework's own loop-blocking detector. LiveKit runs one job process per
    call, so a fresh process pays it EVERY call, not once.

    Called from `setup_fnc`, which runs while a pre-warmed process sits idle
    with nobody on the phone. Same 888ms, spent where it costs nothing.

    WHY IT IS NOT JUST A TOP-LEVEL IMPORT. Hoisting `import opik` to module
    scope would also remove the stall -- and would break the claim this whole
    seam exists to make. Deleting `opik_integration.py` must leave a working
    agent, and a module-level import makes that false. Here, a missing package
    or a deleted module means the prewarm quietly does nothing and the agent
    runs exactly as before.

    DO NOT "TIDY THIS AWAY". It looks like an import that does nothing, because
    that is precisely what a warm-up is. Removing it silently reintroduces a
    ~0.9s silence after every patient says hello.
    """
    if not opik_enabled():
        return
    try:
        try:
            from .opik_integration import OpikSink  # noqa: F401
        except ImportError:
            from src.opik_integration import OpikSink  # noqa: F401  (script mode)
    except Exception:
        # Debug, not warning. A prewarm that cannot run is not a problem --
        # build_sink() will try again and report properly if it matters.
        logger.debug("observability prewarm skipped; the sink will load on first use")
        return
    logger.debug("observability prewarm complete")


def build_sink() -> GuardedSink:
    """The single selection point. Everything else takes whatever this returns.

    The Opik branch lives here and nowhere else. Note what is absent from the
    module scope above: no Opik import, no Opik symbol. The import below is
    inside the branch on purpose, so a missing package or a deleted
    `opik_integration.py` degrades to the no-op sink instead of breaking the
    agent -- loudly, because silence would read as "my traces are being sent".
    """
    if not opik_enabled():
        logger.debug("observability: OPIK_ENABLED is not set, using the no-op sink")
        return GuardedSink(NoOpSink())

    # The import is INSIDE the branch, not at module scope. That is what makes
    # "delete opik_integration.py and the agent still runs" true rather than
    # aspirational: with Opik switched off the module is never imported, and the
    # package need not even be installed.
    try:
        from .opik_integration import OpikSink
    except ImportError:
        try:
            from src.opik_integration import OpikSink  # script-mode fallback
        except ImportError:
            logger.exception(
                "OPIK_ENABLED is set but the Opik sink could not be imported "
                "(is `opik` installed?); falling back to the no-op sink. "
                "NOTHING IS BEING EXPORTED."
            )
            return GuardedSink(NoOpSink())

    try:
        sink = OpikSink()
    except Exception:
        # A bad API key or an unreachable host surfaces here. Loud, and then the
        # call proceeds -- observability is never worth dropping a patient call.
        logger.exception(
            "OPIK_ENABLED is set but the Opik client could not be constructed; "
            "falling back to the no-op sink. NOTHING IS BEING EXPORTED."
        )
        return GuardedSink(NoOpSink())

    logger.info("observability: exporting to Opik")
    return GuardedSink(sink)
