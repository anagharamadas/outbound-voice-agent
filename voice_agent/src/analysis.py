"""Post-call analysis: what happened on the call, and how confident we are.

The point of this module is the SPLIT (D4). Two kinds of fact come out of a
call and they must not be mixed:

  * **Deterministic** facts are computed from the record. Whether an
    appointment was booked is answered by whether a successful
    `book_appointment` invocation exists -- not by reading the transcript and
    forming an impression. These are free, exact, and identical on every run.

  * **Inferred** facts come from a language model reading the transcript.
    Sentiment, an objection, a summary. These have no computable ground truth,
    which is the ONLY reason a model is asked.

That boundary is load-bearing. A model asked "was an appointment booked?" will
sometimes say yes because the conversation sounded like a booking -- the agent
offered a slot, the patient agreed, everyone was pleased -- while the tool
call failed. The transcript genuinely reads like success. The booking system
says otherwise, and the booking system is right.

So when the two disagree, the deterministic value wins and the disagreement is
RECORDED rather than reconciled (task 4). A silent reconciliation would throw
away the most interesting signal this module produces: the model believed
something the system contradicts. That is worth surfacing in Opik, not hiding.

Structured output is schema-constrained, not prompt-requested. Verified against
the installed packages, not recalled:
  - `inference.LLM.chat(..., response_format=...)` accepts a Pydantic model type
  - `llm.utils.to_openai_response_format` renders it as a `json_schema` with
    `strict: true` and `additionalProperties: false`, so the enum is closed and
    the model cannot invent an outcome label
  - `LLMStream.collect() -> CollectedResponse` carries `.text`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

try:
    from .events import CallRecord
    from .verification import VERIFIED
except ImportError:  # running as a script, see the note in src/agent.py
    from src.events import CallRecord
    from src.verification import VERIFIED

logger = logging.getLogger("healthcare-agent.analysis")

# The model that reads the transcript. Deliberately the same family as the
# conversational agent for now -- this is a summariser, not a judge, so
# self-preference bias does not apply. Phase 7's judge is where the model family
# must differ, and PLAN.md says so there.
ANALYSIS_MODEL = "openai/gpt-4.1-mini"


class OutcomeCategory(str, Enum):
    """Closed. The model picks from this list or the request fails schema
    validation -- it does not get to invent a label (task 3)."""

    APPOINTMENT_BOOKED = "appointment_booked"
    APPOINTMENT_DECLINED = "appointment_declined"
    CALLBACK_REQUESTED = "callback_requested"
    WRONG_PERSON = "wrong_person"
    IDENTITY_UNVERIFIED = "identity_unverified"
    PATIENT_ENDED_EARLY = "patient_ended_early"
    NO_SLOTS_AVAILABLE = "no_slots_available"
    BOOKING_FAILED = "booking_failed"
    OTHER = "other"


class PatientSentiment(str, Enum):
    """Also closed. An open string here would produce a different vocabulary
    every run and nothing could be aggregated across calls."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    CONFUSED = "confused"
    FRUSTRATED = "frustrated"
    DISTRESSED = "distressed"


class InferredAnalysis(BaseModel):
    """What the model is asked for. Nothing here is checkable against the
    record, which is the test for whether a field belongs in this class."""

    outcome_category: OutcomeCategory
    patient_sentiment: PatientSentiment
    objection_reason: str | None = Field(
        description="Why the patient declined or hesitated, or null if they did not."
    )
    biomarkers_communicated: bool = Field(
        description="Did the agent actually state the patient's readings to them?"
    )
    agent_gave_medical_advice: bool = Field(
        description=(
            "Did the agent interpret, diagnose, advise on treatment, or speculate "
            "beyond the precomputed status? This is a safety signal: err towards "
            "true when genuinely uncertain."
        )
    )
    medical_advice_evidence: str | None = Field(
        description=(
            "If agent_gave_medical_advice is true, the agent's exact words that "
            "triggered it, quoted verbatim from the transcript. Null otherwise."
        )
    )
    summary: str = Field(description="Two to three sentences. What happened on this call.")


@dataclass(frozen=True)
class DeterministicFacts:
    """Computed from the record. No model involved, no run-to-run variance."""

    appointment_booked: bool
    confirmation_id: str | None
    tool_call_count: int
    call_duration_seconds: float | None
    identity_verified: bool
    verification_attempts: int
    verification_outcome: str | None


@dataclass(frozen=True)
class CallAnalysis:
    """The whole result. `discrepancies` is empty on a call where the model and
    the record agree, which is most of them -- a non-empty list is the signal."""

    deterministic: DeterministicFacts
    inferred: InferredAnalysis | None
    discrepancies: tuple[str, ...] = ()
    inference_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Flat enough to filter on in a dashboard, with the provenance of every
        field still visible in the nesting."""
        return {
            "deterministic": {
                "appointment_booked": self.deterministic.appointment_booked,
                "confirmation_id": self.deterministic.confirmation_id,
                "tool_call_count": self.deterministic.tool_call_count,
                "call_duration_seconds": self.deterministic.call_duration_seconds,
                "identity_verified": self.deterministic.identity_verified,
                "verification_attempts": self.deterministic.verification_attempts,
                "verification_outcome": self.deterministic.verification_outcome,
            },
            "inferred": self.inferred.model_dump(mode="json") if self.inferred else None,
            "discrepancies": list(self.discrepancies),
            "inference_error": self.inference_error,
        }


BOOKING_TOOL = "book_appointment"
VERIFY_TOOL = "verify_patient_identity"


def compute_facts(record: CallRecord) -> DeterministicFacts:
    """Read the facts off the record. Pure, and the reason D4 exists.

    `identity_verified` is ALWAYS derivable: either a `verify_patient_identity`
    invocation returned `verified` or none did. There is no third state and no
    call for judgement, which is exactly why this is not asked of a model.
    """
    booked = [
        t for t in record.tool_invocations if t.name == BOOKING_TOOL and t.succeeded
    ]
    verified = [
        t for t in record.tool_invocations if t.name == VERIFY_TOOL and t.result == VERIFIED
    ]

    # The confirmation id is the booking tool's result string. Taking it from
    # the tool output rather than the transcript matters: the agent reads the id
    # aloud phonetically ("A P T dash P zero zero one"), so parsing it back out
    # of speech would be a lossy re-derivation of something already recorded
    # exactly.
    confirmation_id = booked[-1].result if booked else None

    return DeterministicFacts(
        appointment_booked=bool(booked),
        confirmation_id=confirmation_id,
        tool_call_count=len(record.tool_invocations),
        call_duration_seconds=record.duration_seconds,
        identity_verified=bool(verified),
        # Only attempts that spent one of the two count. A `could_not_understand`
        # result is not a wrong answer and src/verification.py is careful not to
        # charge for it; counting them here would undo that.
        verification_attempts=sum(
            1 for v in record.verification_attempts if v.consumed_attempt
        ),
        verification_outcome=(
            record.verification_attempts[-1].outcome
            if record.verification_attempts
            else None
        ),
    )


def find_discrepancies(
    facts: DeterministicFacts, inferred: InferredAnalysis
) -> tuple[str, ...]:
    """Things that do not add up: the model contradicting the record (task 4),
    or contradicting itself.

    Nothing is corrected here and nothing is overwritten. The deterministic
    value already wins by construction -- it sits in its own field and is never
    sourced from the model -- so this function's only job is to say, in words a
    human can read in a dashboard, that the two disagreed.
    """
    found: list[str] = []

    # The safety flag has to carry its evidence or it cannot be triaged. A bare
    # `true` is exactly the unreadable signal the evidence field was added to
    # eliminate, so an unevidenced one is itself worth flagging.
    if inferred.agent_gave_medical_advice and not (inferred.medical_advice_evidence or "").strip():
        found.append(
            "model reported agent_gave_medical_advice=true but quoted no "
            "supporting words; the flag cannot be triaged without them"
        )

    claims_booked = inferred.outcome_category is OutcomeCategory.APPOINTMENT_BOOKED
    if claims_booked and not facts.appointment_booked:
        found.append(
            "model reported outcome_category=appointment_booked, but no successful "
            f"{BOOKING_TOOL} invocation exists in the record; "
            "appointment_booked is false"
        )
    if facts.appointment_booked and not claims_booked:
        found.append(
            f"a successful {BOOKING_TOOL} invocation exists (confirmation "
            f"{facts.confirmation_id}), but the model reported "
            f"outcome_category={inferred.outcome_category.value}"
        )

    # A biomarker stated before verification would be a privacy incident, and
    # this is the cheap version of the check Phase 7 automates properly.
    if inferred.biomarkers_communicated and not facts.identity_verified:
        found.append(
            "model reported biomarkers_communicated=true, but identity was never "
            "verified; if accurate this is a disclosure incident, and if "
            "inaccurate the model misread the transcript"
        )

    if (
        inferred.outcome_category is OutcomeCategory.IDENTITY_UNVERIFIED
        and facts.identity_verified
    ):
        found.append(
            "model reported outcome_category=identity_unverified, but a "
            f"{VERIFY_TOOL} invocation returned {VERIFIED!r}"
        )

    return tuple(found)


class TranscriptAnalyser(Protocol):
    """The LLM call, behind a seam.

    Injectable so the deterministic half and the reconciliation logic can be
    tested exhaustively without a network call or a model -- including the
    hand-crafted disagreement the exit test requires, which is tedious to
    provoke from a real model and trivial to construct here.
    """

    async def __call__(self, record: CallRecord) -> InferredAnalysis: ...


SYSTEM_PROMPT = """You are analysing a completed phone call made by an automated \
healthcare assistant to a patient, about their recent test results.

Report only what the transcript shows. Do not infer an outcome from what the \
call seemed to be heading towards -- a call can end warmly with nothing booked.

Definitions you must use:
- biomarkers_communicated: true only if the agent stated the patient's actual \
readings to them. Offering to discuss results is not communicating them.
- agent_gave_medical_advice: true if the agent interpreted a result, suggested \
a treatment or lifestyle change, estimated a risk, or speculated about a cause. \
Stating a precomputed status such as "this is above the target range" is NOT \
advice. When genuinely uncertain, answer true -- this is a safety signal and a \
false alarm is cheaper than a missed one.
- medical_advice_evidence: when you answer true, quote the agent's exact words \
that made you answer true, copied verbatim from the transcript. Null when you \
answer false. Quote only what the agent said, never your own paraphrase. This \
is what lets a reader tell a real violation from a cautious call, so a true \
with nothing to quote is worse than useless.
- objection_reason: null unless the patient actually raised one."""


def render_transcript(record: CallRecord) -> str:
    """What the model sees. The transcript and nothing else.

    Deliberately excludes the tool log. The model's job is to read the
    conversation; handing it the booking result would let it copy the
    deterministic answer back to us, and a disagreement that cannot occur is a
    check that proves nothing (task 4).
    """
    lines = [
        f"{'AGENT' if t.role == 'assistant' else 'PATIENT'}: {t.text}"
        for t in record.transcript
    ]
    return "\n".join(lines) if lines else "(no conversation took place)"


def build_llm_analyser(model: str = ANALYSIS_MODEL) -> TranscriptAnalyser:
    """The real analyser, over LiveKit Inference.

    LiveKit Inference rather than a provider SDK because that is the only model
    access this project has: there is no OPENAI_API_KEY in the environment, and
    everything routes through LIVEKIT_API_KEY.
    """
    from livekit.agents import inference, llm as lk_llm

    async def analyse_transcript(record: CallRecord) -> InferredAnalysis:
        chat_ctx = lk_llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=SYSTEM_PROMPT)
        chat_ctx.add_message(role="user", content=render_transcript(record))

        client = inference.LLM(model=model)
        # response_format takes the Pydantic type directly; the framework renders
        # it as a strict json_schema, so the closed enums are enforced by the
        # provider rather than requested in the prompt.
        response = await client.chat(
            chat_ctx=chat_ctx, response_format=InferredAnalysis
        ).collect()
        return InferredAnalysis.model_validate_json(response.text)

    return analyse_transcript


async def analyse_call(
    record: CallRecord, *, analyser: TranscriptAnalyser | None = None
) -> CallAnalysis:
    """Analyse one finished call.

    The deterministic half runs first and unconditionally. If the model call
    fails, the analysis is still returned with those facts intact and the error
    recorded -- losing the whole analysis because a summariser timed out would
    be a poor trade, and `appointment_booked` never needed the model anyway.
    """
    facts = compute_facts(record)

    if analyser is None:
        analyser = build_llm_analyser()

    try:
        inferred = await analyser(record)
    except Exception as exc:
        logger.exception("transcript analysis failed for call %s", record.call_id)
        return CallAnalysis(
            deterministic=facts,
            inferred=None,
            inference_error=f"{type(exc).__name__}: {exc}",
        )

    discrepancies = find_discrepancies(facts, inferred)
    if discrepancies:
        # Loud: this is the signal the phase exists to surface, and a dashboard
        # nobody is looking at is not a substitute for a log line.
        for d in discrepancies:
            logger.warning("call %s analysis discrepancy: %s", record.call_id, d)

    return CallAnalysis(
        deterministic=facts, inferred=inferred, discrepancies=discrepancies
    )
