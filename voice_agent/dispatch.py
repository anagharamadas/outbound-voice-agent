#!/usr/bin/env python3
"""CLI entrypoint: dial one patient.

Two steps, in this order and for a reason:

  1. Dispatch the agent into a fresh room. It is waiting before anyone picks up,
     so the opening line is ready the moment the call connects. Dialling first
     would risk a patient answering to silence.
  2. Place the outbound SIP call into that same room.

DIALLING IS OPT-IN. Without `--call` this loads the record, prints the summary
and stops -- the Phase 1 behaviour, preserved deliberately. A tool that bills
per minute and rings a real phone in India should not do so because someone
pressed up-arrow and enter.

ONE DIAL ATTEMPT PER INVOCATION. No retry, no backoff, no loop, anywhere in
this file (hard rule 4). Bursts of short calls can trigger carrier-side
blocking, and every connected call bills as a rounded whole minute. A failed
call is reported and the process exits; re-dialling is a human decision.

PREREQUISITE: a worker must already be running and registered under
`AGENT_NAME`, otherwise the dispatch succeeds and nobody joins the room:

    ./venv/bin/python src/agent.py start
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from livekit import api
from livekit.protocol.sip import CreateSIPParticipantRequest

from src.agent import AGENT_NAME
from src.config import Config, ConfigError, load_config
from src.patient import Patient, PatientDataError, get_patient
from src.telephony import diagnose, mask

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
    return mask(number)


def _room_name(patient_id: str) -> str:
    """Unique per invocation, and readable in the LiveKit dashboard.

    Reusing a room across calls would let a late-arriving participant from a
    previous call land in this one, which on a healthcare call means the wrong
    person hearing someone else's results.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"call-{patient_id}-{stamp}"


async def place_call(patient: Patient, config: Config) -> int:
    """Dispatch the agent, then dial. Exactly one attempt."""
    room = _room_name(patient.patient_id)
    destination = patient.identity.phone_number

    print(f"  room        : {room}")
    print(f"  agent       : {AGENT_NAME}")
    print(f"  dialling    : {_mask_phone(destination)}")
    print()

    lkapi = api.LiveKitAPI()
    try:
        # STEP 1 -- the agent joins first and waits.
        #
        # The patient id travels as dispatch metadata rather than an env var, so
        # one long-running worker can serve calls to different patients without
        # a restart. The agent reads it in load_target_patient().
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room,
                metadata=json.dumps({"patient_id": patient.patient_id}),
            )
        )
        print("  agent dispatched, waiting in the room")

        # STEP 2 -- one dial. `wait_until_answered` makes a failure surface here
        # as an exception rather than as a silent room nobody joins.
        request = CreateSIPParticipantRequest(
            sip_trunk_id=config.livekit_outbound_trunk_id,
            sip_call_to=destination,
            room_name=room,
            participant_identity=f"patient-{patient.patient_id}",
            participant_name=patient.identity.name,
            wait_until_answered=True,
        )
        print("  dialling now (blocking until answered; Ctrl-C to abandon)")
        participant = await lkapi.sip.create_sip_participant(request)

    except api.SipCallError as exc:
        # Single attempt. Report and exit -- never re-dial (hard rule 4).
        print("\nCALL FAILED", file=sys.stderr)
        print(f"  sip_status_code : {exc.sip_status_code}", file=sys.stderr)
        print(f"  sip_status      : {exc.sip_status}", file=sys.stderr)
        print(f"  error           : {exc}", file=sys.stderr)
        print(f"\nDiagnosis: {diagnose(exc.sip_status_code, exc.sip_status, exc)}",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface anything else verbatim
        print("\nCALL FAILED (non-SIP error)", file=sys.stderr)
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"\nDiagnosis: {diagnose(None, None, exc)}", file=sys.stderr)
        return 1
    else:
        print()
        print("ANSWERED -- the agent is now talking to the patient.")
        print(f"  participant_id : {participant.participant_id}")
        print(f"  sip_call_id    : {participant.sip_call_id}")
        print(f"  room_name      : {participant.room_name}")
        print()
        print("The call record, analysis and Opik trace are written by the AGENT")
        print("process when the call ends -- watch the worker's log, not this one.")
        return 0
    finally:
        await lkapi.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dial one patient. Prints the record; add --call to actually dial."
    )
    parser.add_argument("--patient-id", required=True, help="e.g. P001")
    parser.add_argument(
        "--call",
        action="store_true",
        help="actually place the call. Without it, this prints the record and exits.",
    )
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
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        patient = get_patient(args.patients_file, args.patient_id)
    except PatientDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_summary(patient)

    if not args.call:
        print("No call placed. Re-run with --call to dial this patient.")
        return 0

    print("Placing ONE outbound call. No retry on failure.")
    print()
    return asyncio.run(place_call(patient, config))


if __name__ == "__main__":
    sys.exit(main())
