"""Agent classes and the worker entrypoint.

Two agent classes implement the verification gate (D10). `UnverifiedAgent` is
constructed WITHOUT health data and WITHOUT booking tools; `VerifiedAgent` is
constructed WITH the health payload and only ever from inside a successful
verification. The gate is therefore a property of what each object was built
with, checkable by reading a constructor, rather than a prompt instruction a
model can drift past.

API surface re-confirmed against the INSTALLED livekit-agents 1.8.2 (PLAN.md
Phase 2 task 1), not against GitHub main:
  - Agent.__init__(*, instructions, chat_ctx=NOT_GIVEN, tools=None, ...)
  - AgentSession.__init__(*, stt, llm, tts, vad, ...) -- accepts model-id
    strings or inference.* component objects
  - AgentSession.start(agent, *, room=..., ...)
  - @function_tool from livekit.agents (Phase 2a)
  - Handoff: returning an Agent from a tool triggers it
    (voice/tool_executor.py:382, `isinstance(output, Agent)`)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    cli,
    function_tool,
    inference,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.agents.llm import ChatMessage, ToolError

try:
    from . import booking
    from .analysis import analyse_call
    from .events import CallRecorder, ToolInvocation, from_epoch, utcnow
    from .patient import Health, Identity, Patient, get_patient
    from .prompts import CLINIC_NAME, unverified_instructions, verified_instructions
    from .sinks import ObservabilitySink, build_sink
    from .verification import (
        ATTEMPTS_EXHAUSTED,
        COULD_NOT_UNDERSTAND,
        MAX_VERIFICATION_ATTEMPTS,
        NOT_VERIFIED,
        VERIFIED,
        VerificationAttempt,
        normalise_patient_id,
        parse_stated_date,
    )
except ImportError:
    # Run as a script rather than as a module. `lk agent console src/agent.py`
    # does exactly this, and without the fallback it hangs on "Starting agent"
    # instead of reporting the import failure. Put the project root on the path
    # so the package imports resolve either way.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import booking
    from src.analysis import analyse_call
    from src.events import CallRecorder, ToolInvocation, from_epoch, utcnow
    from src.patient import Health, Identity, Patient, get_patient
    from src.prompts import CLINIC_NAME, unverified_instructions, verified_instructions
    from src.sinks import ObservabilitySink, build_sink
    from src.verification import (
        ATTEMPTS_EXHAUSTED,
        COULD_NOT_UNDERSTAND,
        MAX_VERIFICATION_ATTEMPTS,
        NOT_VERIFIED,
        VERIFIED,
        VerificationAttempt,
        normalise_patient_id,
        parse_stated_date,
    )

logger = logging.getLogger("healthcare-agent")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PATIENTS_FILE = PROJECT_ROOT / "data" / "patients.json"

# Where the finished CallRecord is dumped for inspection. This is NOT the
# observability sink -- it is written whatever sink is configured, which is how
# the Phase 4 exit test can show a complete record with observability switched
# off entirely. Gitignored: these files hold biomarkers and a full transcript.
CALL_RECORDS_DIR = PROJECT_ROOT / "call_records"

# The post-call analysis is an LLM call, and it runs inside a job shutdown
# callback. VERIFIED on the installed 1.8.2: `AgentServer` defaults
# `shutdown_process_timeout` to 10.0s and the supervisor kills the process when
# callbacks overrun it (`job_proc_lazy_main` gathers them with no timeout of its
# own -- its source comment notes that a hung callback is exactly how jobs hit
# that deadline). Measured on a real 15-turn call the analysis took 2.1-4.4s, so
# it fits, but not with room to spare: the same budget also covers the sink
# emission, which gains an Opik flush in Phase 6. Hence a hard cap well under
# the deadline, and an ordering that spends the budget on the record FIRST.
ANALYSIS_TIMEOUT_SECONDS = 6.0

# Where call audio lands. `lk agent console --record` writes to
# `console-recordings/` relative to the directory it was launched from; Phase 8
# will point this at whatever egress produces for a real call. Override with
# CALL_AUDIO_DIR.
#
# CONFIRMED by a real `--record` run on 2026-09-19: `lk` writes
#   console-recordings/session-<MM-DD-HHMMSS>/audio.ogg
# alongside a session_report.json. So the format is OGG, not WAV -- which is why
# this accepts both rather than assuming, and why the search must RECURSE: the
# audio sits one directory down, and a flat scan finds only the session folder.
# `audio/vorbis` is a previewable attachment type in Opik, so .ogg is usable in
# Phase 6 as-is and needs no transcoding.
AUDIO_EXTENSIONS = (".wav", ".ogg")

# The recording lands at almost exactly the moment the job shuts down. Measured
# on a real recorded call: audio.ogg was written at 22:40:15 and the shutdown
# callback ran within that same second. Filesystem timestamps are not finer than
# a second, so whether the file is closed before or after the lookup is a
# coin-flip -- hence a short poll rather than a single glance. It is deliberately
# small: 1.5s plus the 6s analysis cap stays well inside the ~10s deadline.
AUDIO_WAIT_SECONDS = 1.5
AUDIO_POLL_SECONDS = 0.25


def call_audio_dir() -> Path:
    configured = (os.getenv("CALL_AUDIO_DIR") or "").strip()
    return Path(configured) if configured else PROJECT_ROOT / "console-recordings"


def find_call_audio(directory: Path, *, not_before: datetime) -> Path | None:
    """The newest audio file written during this call, if there is one.

    Matched on modification time rather than filename because the recording is
    written by the console host, not by this process, and its naming is not
    something this code should assume. `not_before` is the call start, so a
    recording left over from a previous call is not picked up.

    Returns None routinely and without complaint: a call recorded with no
    --record flag has no audio, and that is a normal state the record already
    models (`audio_path` is optional).

    NOTE the ordering hazard -- the console host may not have finished writing
    when the agent's shutdown callback runs, in which case this correctly finds
    nothing. That is why a missing file is not treated as an error.
    """
    if not directory.is_dir():
        return None
    cutoff = not_before.timestamp()
    # rglob, not iterdir: `lk` nests the audio inside a per-session folder, so a
    # flat scan sees the folder and no audio at all. This was a real miss on the
    # first recorded call.
    candidates = [
        f
        for f in directory.rglob("*")
        if f.is_file()
        and f.suffix.lower() in AUDIO_EXTENSIONS
        and f.stat().st_mtime >= cutoff
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)


async def wait_for_call_audio(directory: Path, *, not_before: datetime) -> Path | None:
    """`find_call_audio`, but give the console host a moment to finish writing.

    Returns as soon as a file appears, so the common cases cost nothing: a call
    recorded with --record usually resolves on the first or second poll, and a
    call with no recording at all pays the full 1.5s exactly once, at shutdown,
    where it is not competing with anything the caller is waiting on.
    """
    # No directory means no recording, and waiting will not conjure one: `lk`
    # creates `console-recordings/session-<start-time>/` when the session STARTS,
    # so by shutdown it either exists or was never going to. Bailing here is what
    # keeps an ordinary unrecorded console run from paying 1.5s for nothing.
    if not directory.is_dir():
        return None

    deadline = asyncio.get_running_loop().time() + AUDIO_WAIT_SECONDS
    while True:
        try:
            found = find_call_audio(directory, not_before=not_before)
        except OSError:
            logger.exception("could not look for call audio; continuing without it")
            return None
        if found is not None:
            return found
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(AUDIO_POLL_SECONDS)

# Set ANALYSIS_ENABLED=false to skip it. Every console run otherwise costs an
# inference call, which is a poor default when iterating on the call flow.
def analysis_enabled() -> bool:
    return (os.getenv("ANALYSIS_ENABLED") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )

# Model IDs are taken from the Literal types in the installed package
# (livekit.agents.inference.STTModels / LLMModels / TTSModels), not from memory.
#
# STT: the medical variant is tuned for clinical vocabulary, which matters once
#      biomarker names are spoken in Phase 2a. Phase 8 should reconsider
#      deepgram/nova-2-phonecall, which is tuned for narrowband telephony.
# LLM: chosen for instruction-following latency on a voice turn. The gate does
#      not depend on the model behaving -- it is enforced in code -- so this is
#      a quality choice, not a safety one.
STT_MODEL = "deepgram/nova-3-medical"
LLM_MODEL = "openai/gpt-4.1-mini"
TTS_MODEL = "inworld/inworld-tts-2"
TTS_VOICE = "Ashley"


def build_end_call_tool() -> EndCallTool:
    """The framework's own hang-up tool.

    This resolves the Phase 0 open question: ending a call is NOT an implicit
    framework capability, it is a tool the model must call, so it is a fourth
    tool and PLAN.md needs updating (Phase 0 recon item 1, last bullet).

    `livekit.agents.beta.tools` is a beta namespace and may move.

    Both strings below are sent to the model, so neither may hint at why the
    call was placed (D12). `end_instructions` is the tool's output, which the
    model uses to word its closing line -- left contentless deliberately, since
    the exit-2 case must end the call without ever stating a purpose.
    """
    return EndCallTool(
        extra_description=(
            "Also call this after you have delivered a closing line because the "
            "person on the line is not the person you asked for, because you were "
            "unable to confirm who you are speaking with, or because they asked "
            "you to stop or to call back later."
        ),
        end_instructions=(
            "Close warmly in one short sentence. Do not state or hint at why the "
            "call was made, and do not leave a message."
        ),
        # The opening is generated in on_enter; without this the model can hang
        # up while greeting.
        ignore_on_enter=True,
        # Disconnects SIP callers too, which is what we want from Phase 8 on.
        delete_room=True,
    )


class VerifiedAgent(Agent):
    """Constructed only after the gate passes. This is the one object in the
    system that holds health data in its model context (D10).

    Note what it is NOT given: the verification values. It never receives the
    date of birth, so it cannot disclose it, confirm it, or be talked into
    hinting at it -- exit-test scenario 6.
    """

    def __init__(
        self,
        *,
        patient_id: str,
        identity: Identity,
        health: Health,
        identity_payload: dict[str, str],
        clinic_name: str = CLINIC_NAME,
        verification_log: list[VerificationAttempt] | None = None,
        tool_log: list[ToolInvocation] | None = None,
    ) -> None:
        super().__init__(
            instructions=verified_instructions(
                identity_payload, health.biomarkers, clinic_name=clinic_name
            ),
            tools=[build_end_call_tool()],
        )
        self._patient_id = patient_id
        self._identity = identity
        self._health = health
        # Both carried across the handoff so the call record is complete (D4).
        # The verification calls happened on the PREVIOUS agent -- without
        # carrying the tool log the record would show a booking with no
        # verification preceding it, which is exactly the shape Phase 7 checks
        # for and would read as a privacy failure that did not occur.
        self.verification_log: list[VerificationAttempt] = list(verification_log or [])
        self.tool_log: list[ToolInvocation] = list(tool_log or [])
        self.confirmation_id: str | None = None

    @function_tool
    async def get_available_slots(self, context: RunContext) -> str:
        """List the appointment times that can be booked.

        Call this before offering any time to the person. Offer only the times
        this returns.
        """
        slots = booking.get_available_slots()
        self._record_tool("get_available_slots", {}, f"{len(slots)} slot(s)", True)
        if not slots:
            return "no_slots_available"
        # The id is what book_appointment takes; the spoken form is what to say.
        return "\n".join(f"{s.slot_id} — {s.spoken()}" for s in slots)

    @function_tool
    async def book_appointment(self, context: RunContext, slot_id: str) -> str:
        """Book one of the available appointment times.

        Only call this with a slot id returned by get_available_slots, and only
        after the person has agreed to that specific time.

        Args:
            slot_id: the identifier of the chosen slot, exactly as it was
                listed by get_available_slots.
        """
        # `slot_id` is an opaque string, deliberately NOT an enum of real times
        # (D12 rule 3): tool schemas are sent to the model, so putting live data
        # in one widens the surface for no benefit. Validation happens in code.
        result = booking.book_appointment(slot_id=slot_id, patient_id=self._patient_id)
        if result.succeeded:
            self._record_tool(
                "book_appointment", {"slot_id": slot_id}, result.confirmation_id, True
            )
            self.confirmation_id = result.confirmation_id
            return (
                f"booked. confirmation_id={result.confirmation_id}. "
                f"Read the confirmation id back to the person, then confirm the "
                f"appointment is with {result.doctor}."
            )
        self._record_tool("book_appointment", {"slot_id": slot_id}, result.reason, False)
        # Tell the model plainly that nothing was booked. A vague failure string
        # invites it to imply success.
        return (
            f"NOT booked. reason={result.reason}. {result.message} "
            f"Tell the person honestly that the appointment was not made. "
            f"Do not give them a confirmation id and do not say it is booked."
        )

    def _record_tool(self, name: str, args: dict[str, str], result: str, ok: bool) -> None:
        """Phase 4: this is the lift booking.ToolCallRecord anticipated.

        The timestamp is the addition. Phase 3 had no use for one; Phase 7's
        premature-disclosure check is a comparison between when verification
        succeeded and when a biomarker was first spoken, and neither side of
        that is answerable without a clock.
        """
        self.tool_log.append(
            ToolInvocation(
                name=name, arguments=args, result=result, at=utcnow(), succeeded=ok
            )
        )

    async def on_enter(self) -> None:
        """Speak as soon as the handoff lands.

        Without this the handed-off agent says nothing until the person speaks
        again -- verified by Phase 2a exit test scenario 2, where the biomarkers
        arrived only after an extra user turn. The handed-off context IS
        effective immediately; what is missing is anything prompting the new
        agent to talk. This is the empirical answer to the recon inference
        recorded in docs/recon.md Part 1.
        """
        await self.session.generate_reply(
            instructions=(
                "Identity is now confirmed. Tell them why you called and give "
                "them their readings, following your instructions."
            )
        )


class UnverifiedAgent(Agent):
    """The agent that answers the phone. Holds no health data in its context.

    The patient's verification values and health payload are held as plain
    attributes on the instance so Phase 2a's tool can reach them through
    `self`. They are deliberately NOT passed to `super().__init__()`, not put
    in `userdata`, and not referenced in any tool schema (D12). The only thing
    that reaches the model is `patient.identity_payload()`.
    """

    def __init__(self, patient: Patient, *, clinic_name: str = CLINIC_NAME) -> None:
        super().__init__(
            instructions=unverified_instructions(
                patient.identity_payload(), clinic_name=clinic_name
            ),
            tools=[build_end_call_tool()],
        )
        # Prompt-visible.
        self._patient_id = patient.patient_id
        self._identity = patient.identity
        self._identity_payload = patient.identity_payload()
        self._clinic_name = clinic_name

        # NOT prompt-visible. Process memory only. The verification tool reaches
        # these through `self`; they never enter instructions, userdata, or a
        # tool schema (D12).
        self._verification = patient.verification
        self._health = patient.health
        self._verification_attempts = 0
        self.verification_log: list[VerificationAttempt] = []
        # Verification is a tool call (D11), so it belongs in the same log as
        # the booking calls -- one timestamped sequence, not two to reconcile.
        self.tool_log: list[ToolInvocation] = []

    async def on_enter(self) -> None:
        """Speak first. This is an outbound call -- we placed it, so the person
        who picks up says nothing until we do (Section 3a, Stage 1).

        The instruction here is deliberately contentless: the opening script
        lives in the system prompt, and repeating any of it here would be a
        second place where a purpose disclosure could creep in.
        """
        await self.session.generate_reply(
            instructions="Give your opening now, exactly as Stage 1 describes."
        )

    @function_tool
    async def verify_patient_identity(
        self, context: RunContext, stated_identifier: str
    ) -> str | Agent:
        """Check an identifier the person has just stated, to confirm who they are.

        Call this as soon as the person states a date of birth or a patient ID.
        Pass their words through exactly as they said them.

        Args:
            stated_identifier: what the person said, word for word, with no
                tidying up or reformatting.
        """
        # The cap lives here and not in the prompt: a model told to "allow two
        # attempts" will sometimes allow four (Section 3a rule 1).
        if self._verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
            self._record(ATTEMPTS_EXHAUSTED, "unrecognised", stated_identifier, False)
            return ATTEMPTS_EXHAUSTED

        stated = (stated_identifier or "").strip()
        stated_date = parse_stated_date(stated)
        stated_id = None if stated_date else normalise_patient_id(stated)

        # Could not parse a complete, unambiguous identifier. NOT a wrong answer,
        # so it must not spend an attempt -- otherwise a bad line rejects a real
        # patient. Safe to distinguish: the reply is the same whatever is on
        # record, so it reveals nothing.
        if stated_date is None and stated_id is None:
            self._record(COULD_NOT_UNDERSTAND, "unrecognised", stated, False)
            logger.info("verification: could not parse stated identifier")
            return COULD_NOT_UNDERSTAND

        kind = "date_of_birth" if stated_date else "patient_id"
        # Exact comparison on parsed values -- see the tolerance policy in
        # src/verification.py.
        matched = (
            stated_date == self._verification.date_of_birth
            if stated_date
            else stated_id == self._patient_id
        )

        if not matched:
            self._verification_attempts += 1
            exhausted = self._verification_attempts >= MAX_VERIFICATION_ATTEMPTS
            outcome = ATTEMPTS_EXHAUSTED if exhausted else NOT_VERIFIED
            self._record(outcome, kind, stated, True)
            logger.info(
                "verification failed (%s), attempt %d of %d",
                kind,
                self._verification_attempts,
                MAX_VERIFICATION_ATTEMPTS,
            )
            # Status only. Never the expected value, never which part was wrong.
            return outcome

        self._record(VERIFIED, kind, stated, True)
        logger.info("verification succeeded (%s) for patient %s", kind, self._patient_id)

        # Returning an Agent is what triggers the handoff
        # (voice/tool_executor.py:382). Fail closed and loudly if construction
        # raises: an agent that believes it verified but holds no data will
        # improvise, which is the worst outcome available.
        try:
            return VerifiedAgent(
                patient_id=self._patient_id,
                identity=self._identity,
                health=self._health,
                identity_payload=self._identity_payload,
                clinic_name=self._clinic_name,
                verification_log=self.verification_log,
                tool_log=self.tool_log,
            )
        except Exception:
            logger.exception("verified agent construction failed after a successful check")
            raise ToolError(
                "Verification succeeded but the call could not continue. "
                "Apologise, ask the person to contact the clinic directly, and end the call."
            ) from None

    def _record(self, outcome: str, kind: str, stated: str, consumed: bool) -> None:
        """Keep the attempt for the Phase 5 record (D4, D11).

        The expected value is never stored -- this record is bound for the
        observability platform.

        Written twice, deliberately, because the two serve different readers.
        `VerificationAttempt` is the domain detail: which identifier kind, which
        attempt number, whether it spent one of the two. `ToolInvocation` is the
        timestamped event, which is what Phase 7 compares biomarker mentions
        against. Deriving one from the other later would mean re-deciding what
        counts as "the moment verification succeeded" at analysis time.
        """
        self.verification_log.append(
            VerificationAttempt(
                outcome=outcome,
                identifier_kind=kind,
                stated=stated,
                attempt_number=self._verification_attempts,
                consumed_attempt=consumed,
            )
        )
        self.tool_log.append(
            ToolInvocation(
                name="verify_patient_identity",
                # What the person said is already in the transcript, so logging
                # it here adds no disclosure. The EXPECTED value is not here.
                arguments={"stated_identifier": stated},
                result=outcome,
                at=utcnow(),
                succeeded=outcome == VERIFIED,
            )
        )


def build_session() -> AgentSession:
    """Cascaded STT -> LLM -> TTS (D1), so the transcript and tool calls are
    discrete and inspectable for the observability and eval phases."""
    return AgentSession(
        stt=inference.STT(model=STT_MODEL),
        llm=inference.LLM(model=LLM_MODEL),
        tts=inference.TTS(model=TTS_MODEL, voice=TTS_VOICE),
        vad=inference.VAD(),
    )


def load_target_patient() -> Patient:
    """Which patient this session is calling.

    In console mode there is no dispatch metadata, so PATIENT_ID selects the
    record. Phase 8 replaces this with data carried on the job.

    .env is loaded here for the reason dispatch.py documents at its own
    `load_config()` call: patients.json may reference ${DESTINATION_PHONE_NUMBER},
    and that has to be in the environment before the record is parsed. The agent
    runs in a SEPARATE worker process that inherits nothing from the dispatcher,
    so the dispatcher having loaded .env does not help here. Without this,
    console mode crashes on P001 -- the default patient -- before reaching any
    agent code.

    `load_dotenv` rather than `load_config`: a console run never dials, so
    demanding a trunk id and a Twilio number would reject a session that has no
    use for either.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    patient_id = (os.getenv("PATIENT_ID") or "P001").strip()
    return get_patient(PATIENTS_FILE, patient_id)


# The default `shutdown_process_timeout` is 10s, sized for an agent that does
# nothing once the call ends. This one does four things -- flush the trace, wait
# for the recording, analyse the transcript, flush again -- and overrunning the
# deadline means the supervisor kills the process mid-flush, losing exactly the
# telemetry the shutdown existed to deliver. Raised deliberately rather than
# squeezing each step into a budget that was never meant to hold them:
#
#   trace flush        <= 3s   (OPIK_FLUSH_TIMEOUT_SECONDS)
#   audio wait         <= 1.5s (AUDIO_WAIT_SECONDS)
#   analysis           <= 6s   (ANALYSIS_TIMEOUT_SECONDS)
#   analysis flush     <= 3s
#   ------------------------
#   worst case         ~13.5s, comfortably inside 30
#
# Every step above is independently capped, so this raises the ceiling without
# removing any of the individual guards.
SHUTDOWN_TIMEOUT_SECONDS = 30.0

server = AgentServer(shutdown_process_timeout=SHUTDOWN_TIMEOUT_SECONDS)


def attach_recorder(session: AgentSession, recorder: CallRecorder) -> None:
    """Subscribe the recorder to the session's own events.

    Both event names are from the installed package's `EventTypes` literal, not
    from memory. `conversation_item_added` carries either a `ChatMessage` or an
    `AgentHandoff`, so the type check is load-bearing rather than defensive --
    the handoff this system performs on every successful verification arrives
    through this same callback.
    """

    @session.on("conversation_item_added")
    def _on_item(event: Any) -> None:
        item = event.item
        if not isinstance(item, ChatMessage) or item.role not in ("user", "assistant"):
            return
        text = item.text_content
        if not text:
            return
        recorder.add_turn(
            role=item.role,
            text=text,
            # The framework's own timestamp, not ours. Ours would be the moment
            # we happened to be notified, which is not when the turn occurred.
            at=from_epoch(item.created_at),
            interrupted=bool(item.interrupted),
        )

    @session.on("close")
    def _on_close(event: Any) -> None:
        reason = getattr(event.reason, "value", event.reason)
        recorder.note_end(reason=str(reason), at=from_epoch(event.created_at))


def write_record(record) -> None:
    """The local inspection artifact. Separate so it can be called twice."""
    try:
        written = record.write_json(CALL_RECORDS_DIR / f"{record.call_id}.json")
        logger.info("call record written to %s", written)
    except OSError:
        # Not fatal: the sink may still deliver. Loud, because the exit test and
        # every later debugging session read this file.
        logger.exception("could not write the local call record")


async def finish_call(
    *,
    recorder: CallRecorder,
    sink: ObservabilitySink,
    agent_holder: Callable[[], Agent],
    reason: str,
) -> None:
    """Build the record, persist it, emit it, then enrich it.

    The ordering is the design, not an accident, and it follows from the ~10s
    shutdown deadline. Cheap and certain first, slow and optional last:

      1. Write the JSON. Costs milliseconds, depends on nothing, and means a
         complete record survives even if everything after this line fails. The
         inspection artifact must not depend on the thing being inspected.
      2. Emit to the sink. This is the trace; it should not queue behind a
         filesystem poll or an LLM call that might time out.
      3. Wait briefly for the recording. The console host writes it as the
         session tears down -- measured on a real call, within the same second
         as this callback -- so a single glance loses a coin-flip. Bounded at
         1.5s and returns the moment it appears.
      4. Analyse. The only genuinely slow step, so it spends what is left of the
         budget rather than the start of it, capped so it cannot take the
         process down with it.
      5. Rewrite, if steps 3 or 4 produced anything.

    Losing the analysis costs a summary and a sentiment label. Losing the record
    costs the call. They are not worth the same, so they are not ordered
    arbitrarily -- which is also why `on_analysis` exists as its own sink method
    rather than the analysis being folded into `on_call_end`.

    NOTE for Phase 6: the audio is therefore NOT known when `on_call_end` fires.
    An Opik sink has to attach it during `on_analysis`, against the trace it
    already created -- `AttachmentClient.upload_attachment` takes an
    `entity_id`, so this is supported, but it is not the shape you would guess
    from reading `on_call_end` alone.
    """
    recorder.note_end(reason=reason)
    agent = agent_holder()
    record = recorder.build(
        verification_attempts=tuple(getattr(agent, "verification_log", ())),
        tool_invocations=tuple(getattr(agent, "tool_log", ())),
    )

    write_record(record)

    result = sink.on_call_end(record)
    if result.delivered:
        logger.info(
            "call %s recorded: %d turn(s), %d tool call(s), end_reason=%s",
            record.call_id,
            len(record.transcript),
            len(record.tool_invocations),
            record.end_reason,
        )
    # The failure branch is not logged here. GuardedSink has already logged it
    # at ERROR with the detail, and a second line would just be noise.

    # Only NOW look for the recording. It is written by the console host as the
    # session tears down, so it is typically not on disk when the record is
    # built -- and waiting for it before writing the record would put a
    # filesystem poll in front of the one artifact that must always survive.
    audio = await wait_for_call_audio(call_audio_dir(), not_before=recorder.started_at)
    if audio is not None:
        record = record.with_audio_path(str(audio))
        logger.info("call audio found: %s", audio)
    else:
        logger.debug("no call audio found for this call")

    analysis = None
    if analysis_enabled():
        try:
            analysis = await asyncio.wait_for(
                analyse_call(record), timeout=ANALYSIS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            # Loud, and then dropped. Overrunning the deadline would have the
            # supervisor kill the process, which is a worse outcome than no
            # summary.
            logger.error(
                "post-call analysis exceeded %.1fs for call %s and was abandoned; "
                "the call record is already written and emitted",
                ANALYSIS_TIMEOUT_SECONDS,
                record.call_id,
            )
        except Exception:
            logger.exception("post-call analysis failed for call %s", record.call_id)
    else:
        logger.info("post-call analysis skipped (ANALYSIS_ENABLED=false)")

    if audio is None and analysis is None:
        return  # nothing new to say; the first write already stands

    # analyse_call never raises on a model failure -- it returns the
    # deterministic facts with `inference_error` set -- so this is still worth
    # attaching even when the inferred half is missing.
    if analysis is not None:
        record = record.with_analysis(analysis.to_dict())
    write_record(record)

    if analysis is not None:
        sink.on_analysis(record, analysis.to_dict())
        logger.info(
            "call %s analysed: booked=%s verified=%s outcome=%s%s",
            record.call_id,
            analysis.deterministic.appointment_booked,
            analysis.deterministic.identity_verified,
            analysis.inferred.outcome_category.value
            if analysis.inferred
            else "unavailable",
            f" DISCREPANCIES={list(analysis.discrepancies)}"
            if analysis.discrepancies
            else "",
        )


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    patient = load_target_patient()
    # Log the id only. The name is patient data and the rest is worse.
    logger.info("starting session for patient %s", patient.patient_id)

    session = build_session()
    agent = UnverifiedAgent(patient)

    recorder = CallRecorder(
        call_id=ctx.job.id,
        room_name=getattr(ctx.room, "name", "") or "",
        patient=patient,
    )
    sink = build_sink()
    sink.on_call_start(recorder.call_id)
    attach_recorder(session, recorder)

    def current_agent() -> Agent:
        """The agent holding the logs at the end of the call.

        After a successful verification this is the `VerifiedAgent`, which
        carries the unverified agent's logs across the handoff. Falling back to
        the agent we started with covers the call that never verified -- and
        `current_agent` raises rather than returning None once the session has
        been torn down, which is precisely when this runs.
        """
        try:
            return session.current_agent
        except RuntimeError:
            return agent

    finalised = False

    async def on_shutdown(reason: str = "job_shutdown") -> None:
        # The job may shut down for reasons that never produced a close event,
        # and a close event does not itself end the job. Both paths lead here,
        # so this must be idempotent.
        nonlocal finalised
        if finalised:
            return
        finalised = True
        await finish_call(
            recorder=recorder, sink=sink, agent_holder=current_agent, reason=reason
        )

    # VERIFIED on the installed livekit-agents 1.8.2: JobContext exposes
    # `add_shutdown_callback`, and a callback whose `co_argcount >= 1` is passed
    # the shutdown reason (job.py, add_shutdown_callback). This is the hook that
    # makes the record survive a process that exits the moment the call ends.
    ctx.add_shutdown_callback(on_shutdown)

    await session.start(agent=agent, room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
