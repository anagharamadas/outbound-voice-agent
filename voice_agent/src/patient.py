"""Patient records, split three ways.

THE SPLIT IS LOAD-BEARING (PLAN.md D10, D12). Each block has a different
destination and they must not be merged:

    identity      -> may enter the model's context at session start
    verification  -> held by the verification tool in process memory ONLY.
                     Never enters a prompt, a chat context, or a tool schema.
    health        -> passed to VerifiedAgent.__init__ ONLY, after the gate passes.

`Patient` deliberately exposes no method that returns all three together.
Anything building a prompt must call `identity_payload()`, which cannot
return a date of birth or a biomarker because it does not touch those
objects. That is the structural guarantee, not a convention.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
DOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENV_REF_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


class PatientDataError(Exception):
    """A patient record is missing, malformed, or fails validation.

    Callers are expected to present this message to the human directly; it is
    written to be read, not to be a traceback.
    """


@dataclass(frozen=True)
class Biomarker:
    name: str
    value: float
    unit: str
    reference_range: str
    status: str  # PRECOMPUTED (D2). The model reads it; it never derives it.


@dataclass(frozen=True)
class Identity:
    """Safe to place in the model's context at session start."""

    name: str
    phone_number: str
    preferred_language: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


@dataclass(frozen=True)
class Verification:
    """NEVER enters a prompt or a chat context. Held by the tool only (D12)."""

    date_of_birth: date


@dataclass(frozen=True)
class Health:
    """Enters the process only via VerifiedAgent.__init__ (D10)."""

    biomarkers: tuple[Biomarker, ...]


@dataclass(frozen=True)
class Patient:
    patient_id: str
    identity: Identity
    verification: Verification
    health: Health

    def identity_payload(self) -> dict[str, str]:
        """The ONLY payload that may be used to build an unverified prompt.

        Reads exclusively from `self.identity` plus the patient id, so it is
        structurally incapable of leaking a date of birth or a biomarker.

        The phone number is excluded deliberately: the dispatcher dials, the
        agent does not need the number, and it is patient PII that would
        otherwise be sent to the model and on to the observability platform.
        """
        return {
            "patient_id": self.patient_id,
            "name": self.identity.name,
            "first_name": self.identity.first_name,
            "preferred_language": self.identity.preferred_language,
        }


def _require(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict):
        raise PatientDataError(f"{where}: expected an object, found {type(obj).__name__}")
    if key not in obj:
        raise PatientDataError(f"{where}: missing required field '{key}'")
    value = obj[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PatientDataError(f"{where}: field '{key}' is empty")
    return value


def _resolve_phone(raw: str, where: str) -> str:
    """Expand a ${VAR} reference so a real number need not be committed.

    PLAN.md Phase 1 says to use the human's own verified number for the patient
    that will be dialled, but data/patients.json is a committed file and hard
    rule 5 keeps personal values out of committed files. A ${VAR} reference
    satisfies both. Literal numbers are passed through unchanged.
    """
    match = ENV_REF_RE.match(raw.strip())
    if not match:
        return raw.strip()
    var = match.group(1)
    value = (os.getenv(var) or "").strip()
    if not value:
        raise PatientDataError(
            f"{where}: phone_number refers to ${{{var}}}, which is missing or "
            f"empty in the environment. Set {var} in .env."
        )
    return value


def _parse_patient(raw: Any, index: int) -> Patient:
    where = f"patient[{index}]"
    patient_id = str(_require(raw, "patient_id", where)).strip()
    where = f"patient '{patient_id}'"

    identity_raw = _require(raw, "identity", where)
    verification_raw = _require(raw, "verification", where)
    health_raw = _require(raw, "health", where)

    phone = _resolve_phone(
        str(_require(identity_raw, "phone_number", f"{where}.identity")),
        f"{where}.identity",
    )
    if not E164_RE.match(phone):
        raise PatientDataError(
            f"{where}.identity: phone_number must be E.164 with a leading '+' "
            f"and 8-15 digits (got {phone!r})"
        )

    identity = Identity(
        name=str(_require(identity_raw, "name", f"{where}.identity")).strip(),
        phone_number=phone,
        preferred_language=str(
            _require(identity_raw, "preferred_language", f"{where}.identity")
        ).strip(),
    )

    dob_raw = str(_require(verification_raw, "date_of_birth", f"{where}.verification")).strip()
    if not DOB_RE.match(dob_raw):
        raise PatientDataError(
            f"{where}.verification: date_of_birth must be YYYY-MM-DD (got {dob_raw!r})"
        )
    try:
        dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise PatientDataError(
            f"{where}.verification: date_of_birth {dob_raw!r} is not a real date ({exc})"
        ) from exc

    biomarkers_raw = _require(health_raw, "biomarkers", f"{where}.health")
    if not isinstance(biomarkers_raw, list) or not biomarkers_raw:
        raise PatientDataError(f"{where}.health: biomarkers must be a non-empty list")

    biomarkers: list[Biomarker] = []
    for i, bm in enumerate(biomarkers_raw):
        bwhere = f"{where}.health.biomarkers[{i}]"
        value = _require(bm, "value", bwhere)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PatientDataError(f"{bwhere}: 'value' must be a number (got {value!r})")
        biomarkers.append(
            Biomarker(
                name=str(_require(bm, "name", bwhere)).strip(),
                value=float(value),
                unit=str(_require(bm, "unit", bwhere)).strip(),
                reference_range=str(_require(bm, "reference_range", bwhere)).strip(),
                status=str(_require(bm, "status", bwhere)).strip(),
            )
        )

    return Patient(
        patient_id=patient_id,
        identity=identity,
        verification=Verification(date_of_birth=dob),
        health=Health(biomarkers=tuple(biomarkers)),
    )


def load_patients(path: Path) -> dict[str, Patient]:
    """Load and validate every record. Raises PatientDataError on any problem."""
    if not path.exists():
        raise PatientDataError(f"patient data file not found: {path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise PatientDataError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise PatientDataError(f"{path}: expected a non-empty JSON array of patients")

    patients: dict[str, Patient] = {}
    for index, item in enumerate(raw):
        patient = _parse_patient(item, index)
        if patient.patient_id in patients:
            raise PatientDataError(f"duplicate patient_id {patient.patient_id!r} in {path}")
        patients[patient.patient_id] = patient
    return patients


def get_patient(path: Path, patient_id: str) -> Patient:
    """Look up one patient, with an error that lists the valid ids."""
    patients = load_patients(path)
    if patient_id not in patients:
        raise PatientDataError(
            f"no patient with id {patient_id!r}. "
            f"Available: {', '.join(sorted(patients))}"
        )
    return patients[patient_id]
