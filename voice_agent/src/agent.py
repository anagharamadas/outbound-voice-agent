"""Agent classes and the worker entrypoint.

PHASE 2 SCOPE: `UnverifiedAgent` only. Its only tool is the framework's
`end_call`, and it holds no health data in its context, so it cannot get past
the identity challenge. That is intended -- the verification tool and
`VerifiedAgent` arrive in Phase 2a.

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

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, inference
from livekit.agents.beta.tools import EndCallTool

try:
    from .patient import Patient, get_patient
    from .prompts import CLINIC_NAME, unverified_instructions
except ImportError:
    # Run as a script rather than as a module. `lk agent console src/agent.py`
    # does exactly this, and without the fallback it hangs on "Starting agent"
    # instead of reporting the import failure. Put the project root on the path
    # so the package imports resolve either way.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.patient import Patient, get_patient
    from src.prompts import CLINIC_NAME, unverified_instructions

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

        # NOT prompt-visible. Process memory only.
        self._verification = patient.verification  # Phase 2a compares against this
        self._health = patient.health  # Phase 2a passes this to VerifiedAgent
        self._verification_attempts = 0

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
