"""A synthetic call record, for suites that must run on a fresh clone.

`call_records/` is gitignored -- it holds real transcripts and biomarkers -- so a
fresh clone has none, and a test suite that REQUIRES one fails for a reviewer
before it has told them anything useful.

A saved record is still preferred when present: it is real data, and it catches
shapes a fixture would not think to produce. This is the floor, not the default.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src.events import CallRecord, CallRecorder, ToolInvocation
from src.patient import get_patient
from src.verification import VERIFIED, VerificationAttempt

PATIENTS_FILE = Path(__file__).resolve().parent / "data" / "patients.json"


def synthetic_record(*, booked: bool = True, verified: bool = True) -> CallRecord:
    """A complete, ordinary call: greeting, verification, results, booking."""
    patient = get_patient(PATIENTS_FILE, "P001")
    rec = CallRecorder(call_id="synthetic-fixture", room_name="fixture", patient=patient)
    t0 = rec.started_at

    rec.add_turn(role="assistant", at=t0,
                 text="This is an automated call from Lakeside Family Clinic. "
                      "May I speak with Meera?")
    rec.add_turn(role="user", text="Speaking.", at=t0 + timedelta(seconds=5))
    rec.add_turn(role="assistant", at=t0 + timedelta(seconds=7),
                 text="Before I continue, could you tell me your date of birth?")
    rec.add_turn(role="user", text="March 22nd 1988.", at=t0 + timedelta(seconds=20))

    tools: list[ToolInvocation] = []
    attempts: list[VerificationAttempt] = []
    if verified:
        tools.append(ToolInvocation(
            name="verify_patient_identity",
            arguments={"stated_identifier": "March 22nd 1988"},
            result=VERIFIED, at=t0 + timedelta(seconds=21), succeeded=True))
        attempts.append(VerificationAttempt(
            outcome=VERIFIED, identifier_kind="date_of_birth",
            stated="March 22nd 1988", attempt_number=0, consumed_attempt=True))
        # Biomarkers are spoken only AFTER verification -- the ordering the
        # Phase 7 metric checks.
        rec.add_turn(role="assistant", at=t0 + timedelta(seconds=23),
                     text="Thank you Meera. Your HbA1c is 7.8 percent, which is above "
                          "the typical range. Would you like to book an appointment?")
        rec.add_turn(role="user", text="Yes please.", at=t0 + timedelta(seconds=35))

    tools.append(ToolInvocation(name="get_available_slots", arguments={},
                                result="3 slot(s)", at=t0 + timedelta(seconds=37),
                                succeeded=True))
    if booked:
        tools.append(ToolInvocation(
            name="book_appointment", arguments={"slot_id": "SLOT-A"},
            result="APT-P001-0001", at=t0 + timedelta(seconds=50), succeeded=True))
        rec.add_turn(role="assistant", at=t0 + timedelta(seconds=52),
                     text="Booked. Your confirmation code is A P T dash P zero zero one "
                          "dash zero zero zero one.")
    rec.note_end(reason="user_initiated")
    return rec.build(verification_attempts=tuple(attempts), tool_invocations=tuple(tools))
