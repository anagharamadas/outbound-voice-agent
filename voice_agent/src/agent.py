"""Agent classes and the worker entrypoint.

PHASE 2 SCOPE: `UnverifiedAgent` only. It has no tools and no health data, so
it cannot get past the identity challenge. That is intended -- the verification
tool and `VerifiedAgent` arrive in Phase 2a.

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
            )
        )
        # Prompt-visible.
        self._patient_id = patient.patient_id
        self._identity = patient.identity

        # NOT prompt-visible. Process memory only.
        self._verification = patient.verification  # Phase 2a compares against this
        self._health = patient.health  # Phase 2a passes this to VerifiedAgent
        self._verification_attempts = 0


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
