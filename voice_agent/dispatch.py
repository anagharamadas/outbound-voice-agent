#!/usr/bin/env python3
"""CLI entrypoint: dial one patient.

PHASE 1 SCOPE: loads and validates the patient record, prints a summary, and
exits. No call is placed and no agent is started -- that arrives in Phase 8.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.patient import Patient, PatientDataError, get_patient

DEFAULT_PATIENTS_FILE = Path(__file__).resolve().parent / "data" / "patients.json"


def _print_summary(patient: Patient) -> None:
    print(f"Patient {patient.patient_id}")
    print()

    print("  identity          -> may enter the model's context at session start")
    print(f"    name              : {patient.identity.name}")
    print(f"    phone_number      : {_mask_phone(patient.identity.phone_number)}")
    print(f"    preferred_language: {patient.identity.preferred_language}")
    print()

    # The value itself is never printed. It is held by the verification tool in
    # process memory and must not reach a prompt, a log, or a terminal (D12).
    print("  verification      -> held by the verification tool ONLY")
    print("    date_of_birth     : <withheld> (present and valid)")
    print()

    print("  health            -> passed to VerifiedAgent.__init__ ONLY, post-gate")
    for bm in patient.health.biomarkers:
        print(
            f"    - {bm.name}: {bm.value} {bm.unit} "
            f"(ref {bm.reference_range}) -- {bm.status}"
        )
    print()

    payload = patient.identity_payload()
    print("  identity_payload() -> the ONLY input to an unverified prompt")
    for key, value in payload.items():
        print(f"    {key}: {value}")
    print()

    # Exit-test assertion, run every time rather than trusted.
    blob = repr(payload).lower()
    leaks: list[str] = []
    if "date_of_birth" in payload or str(patient.verification.date_of_birth) in blob:
        leaks.append("date_of_birth")
    for bm in patient.health.biomarkers:
        if bm.name.lower() in blob or bm.status.lower() in blob:
            leaks.append(f"biomarker:{bm.name}")
    if leaks:
        raise SystemExit(f"GATE VIOLATION: identity_payload() leaked {', '.join(leaks)}")
    print("  gate check: identity_payload() contains no date of birth and no biomarker  OK")


def _mask_phone(number: str) -> str:
    return number if len(number) <= 7 else f"{number[:3]}{'*' * (len(number) - 7)}{number[-4:]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dial one patient. Phase 1: load and print only, no call placed."
    )
    parser.add_argument("--patient-id", required=True, help="e.g. P001")
    parser.add_argument(
        "--patients-file",
        type=Path,
        default=DEFAULT_PATIENTS_FILE,
        help=f"default: {DEFAULT_PATIENTS_FILE}",
    )
    args = parser.parse_args(argv)

    # Config is loaded before the patient because patients.json may reference
    # ${DESTINATION_PHONE_NUMBER}, which load_dotenv() must have populated.
    try:
        load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        patient = get_patient(args.patients_file, args.patient_id)
    except PatientDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_summary(patient)
    print("Phase 1: no call placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
