"""Mock appointment backend.

Plain functions with no LLM concern, as PLAN.md Phase 3 specifies. Nothing here
imports the agent framework, so the booking rules can be tested on their own.

No network and no persistence beyond process memory. A real backend would query
a calendar; the shape of these functions is what matters, not the source.

D7: booking must be able to FAIL and to return NO SLOTS. A tool that always
succeeds proves nothing and gives the Phase 7 evaluation nothing to measure.
Both are triggered deterministically through BOOKING_MODE so they can be
demonstrated on purpose rather than waited for:

    BOOKING_MODE=normal    (default) slots offered, booking succeeds
    BOOKING_MODE=no_slots            get_available_slots returns ()
    BOOKING_MODE=fail                slots offered, booking always fails

There is also a standing failure that needs no configuration: booking a slot id
that was never offered returns `unknown_slot`. That is the realistic guard --
the model inventing a time is the failure mode most likely to occur in practice.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# Fixed roster. Deterministic by construction: same doctors, same weekday
# offsets, same times on every call.
_OFFERED = (
    ("SLOT-A", "Dr Rao", 2, time(10, 0)),
    ("SLOT-B", "Dr Rao", 3, time(15, 30)),
    ("SLOT-C", "Dr Menon", 5, time(9, 15)),
)

_CONFIRMATION_PREFIX = "APT"


@dataclass(frozen=True)
class Slot:
    slot_id: str
    doctor: str
    starts_at: datetime

    def spoken(self) -> str:
        """How a person would say it out loud. No abbreviations, no symbols."""
        hour = self.starts_at.strftime("%I").lstrip("0")
        minute = self.starts_at.strftime("%M")
        meridiem = "in the morning" if self.starts_at.hour < 12 else "in the afternoon"
        clock = hour if minute == "00" else f"{hour} {minute}"
        return f"{self.starts_at.strftime('%A %d %B')} at {clock} {meridiem} with {self.doctor}"


@dataclass(frozen=True)
class BookingConfirmation:
    confirmation_id: str
    slot_id: str
    doctor: str
    starts_at: datetime
    succeeded: bool = True


@dataclass(frozen=True)
class BookingFailure:
    reason: str  # closed set: see REASONS
    message: str
    succeeded: bool = False


REASONS = ("unknown_slot", "slot_taken", "backend_unavailable")


def _mode() -> str:
    return (os.getenv("BOOKING_MODE") or "normal").strip().lower()


def get_available_slots(*, today: date | None = None) -> tuple[Slot, ...]:
    """The slots on offer. May legitimately be empty (D7)."""
    if _mode() == "no_slots":
        return ()
    anchor = today or date.today()
    return tuple(
        Slot(slot_id=sid, doctor=doctor, starts_at=datetime.combine(anchor + timedelta(days=d), t))
        for sid, doctor, d, t in _OFFERED
    )


def book_appointment(
    *, slot_id: str, patient_id: str, today: date | None = None
) -> BookingConfirmation | BookingFailure:
    """Book one slot. Returns a confirmation or a STRUCTURED failure, never raises.

    The caller must check `.succeeded` -- the two results are deliberately
    different types so a caller cannot read a confirmation id off a failure.
    """
    slot_id = (slot_id or "").strip().upper()

    if _mode() == "fail":
        return BookingFailure(
            reason="backend_unavailable",
            message="The booking system did not accept the appointment.",
        )

    available = {s.slot_id: s for s in get_available_slots(today=today)}
    slot = available.get(slot_id)
    if slot is None:
        # Most likely cause: the model offered a time nobody published.
        return BookingFailure(
            reason="unknown_slot",
            message="That appointment time is not one of the available slots.",
        )

    # Deterministic, needs no configuration: this slot is always already taken,
    # so a demo can show the honest-failure path without restarting anything.
    if slot_id == "SLOT-B":
        return BookingFailure(
            reason="slot_taken",
            message="That appointment time has just been taken by someone else.",
        )

    # hashlib, not hash(): Python randomises string hashing per process, so
    # hash() would hand out a different confirmation id on every run.
    seed = f"{slot_id}:{patient_id}".encode()
    digest = int(hashlib.sha256(seed).hexdigest()[:8], 16) % 10000
    return BookingConfirmation(
        confirmation_id=f"{_CONFIRMATION_PREFIX}-{patient_id}-{digest:04d}",
        slot_id=slot.slot_id,
        doctor=slot.doctor,
        starts_at=slot.starts_at,
    )
