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

import logging
import os
from pathlib import Path

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
from livekit.agents.llm import ToolError

try:
    from . import booking
    from .patient import Health, Identity, Patient, get_patient
    from .prompts import CLINIC_NAME, unverified_instructions, verified_instructions
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
    from src.patient import Health, Identity, Patient, get_patient
    from src.prompts import CLINIC_NAME, unverified_instructions, verified_instructions
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

PATIENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "patients.json"

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
        # Carried across the handoff so the Phase 5 record is complete (D4).
        self.verification_log: list[VerificationAttempt] = list(verification_log or [])
        # Phase 3 task 3: every booking tool call and its result.
        self.tool_log: list[booking.ToolCallRecord] = []
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
        """Phase 3 task 3. Phase 4 lifts this into the CallRecord."""
        self.tool_log.append(
            booking.ToolCallRecord(name=name, arguments=args, result=result, succeeded=ok)
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
    """
    patient_id = (os.getenv("PATIENT_ID") or "P001").strip()
    return get_patient(PATIENTS_FILE, patient_id)


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    patient = load_target_patient()
    # Log the id only. The name is patient data and the rest is worse.
    logger.info("starting session for patient %s", patient.patient_id)

    session = build_session()
    await session.start(agent=UnverifiedAgent(patient), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
