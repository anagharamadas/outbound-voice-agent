#!/usr/bin/env python3
"""
Place exactly ONE outbound SIP call, with no agent attached.

Purpose: prove the telephony layer works in isolation. LiveKit dials out through
the Twilio Elastic SIP trunk and the destination handset rings. Nothing is
listening and nothing speaks -- silence on the line is the successful outcome.

There is deliberately NO retry, NO backoff and NO automatic re-dial anywhere in
this file. Bursts of short-duration calls to India can trigger carrier-side
blocking, and every connected call bills as a rounded whole minute.
One invocation == one call attempt, then exit.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.sip import CreateSIPParticipantRequest

# Shared with dispatch.py so the two cannot drift apart (Phase 8 task 3).
from src.telephony import diagnose, mask

ROOM_NAME = "sip-test"
PARTICIPANT_IDENTITY = "sip-test"
PARTICIPANT_NAME = "Telephony smoke test"



async def main() -> int:
    load_dotenv()

    trunk_id = (os.getenv("LIVEKIT_OUTBOUND_TRUNK_ID") or "").strip()
    destination = (os.getenv("DESTINATION_PHONE_NUMBER") or "").strip()

    missing = [
        name
        for name, value in (
            ("LIVEKIT_OUTBOUND_TRUNK_ID", trunk_id),
            ("DESTINATION_PHONE_NUMBER", destination),
            ("LIVEKIT_URL", os.getenv("LIVEKIT_URL")),
            ("LIVEKIT_API_KEY", os.getenv("LIVEKIT_API_KEY")),
            ("LIVEKIT_API_SECRET", os.getenv("LIVEKIT_API_SECRET")),
        )
        if not value
    ]
    if missing:
        print(f"ERROR: missing or empty in .env: {', '.join(missing)}", file=sys.stderr)
        if "LIVEKIT_OUTBOUND_TRUNK_ID" in missing:
            print(
                "Create the trunk first with ./create_trunk.sh, then paste the ST_... id into .env.",
                file=sys.stderr,
            )
        return 2

    if not destination.startswith("+"):
        print(
            "ERROR: DESTINATION_PHONE_NUMBER must be full E.164 with a leading +",
            file=sys.stderr,
        )
        return 2

    # LiveKitAPI() reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from
    # the environment, which load_dotenv() has just populated.
    lkapi = api.LiveKitAPI()

    request = CreateSIPParticipantRequest(
        sip_trunk_id=trunk_id,
        sip_call_to=destination,
        room_name=ROOM_NAME,
        participant_identity=PARTICIPANT_IDENTITY,
        participant_name=PARTICIPANT_NAME,
        wait_until_answered=True,
    )

    print(f"Dialing  {mask(destination)}")
    print(f"Trunk    {trunk_id}")
    print(f"Room     {ROOM_NAME}")
    print("No agent attached -- silence on the line is success.")
    print("Blocking until answered (wait_until_answered=True). Ctrl-C to abandon.\n")

    try:
        participant = await lkapi.sip.create_sip_participant(request)
    except api.SipCallError as e:
        # Single attempt only. Report and exit -- never re-dial.
        print("\nCALL FAILED", file=sys.stderr)
        print(f"  sip_status_code : {e.sip_status_code}", file=sys.stderr)
        print(f"  sip_status      : {e.sip_status}", file=sys.stderr)
        print(f"  error           : {e}", file=sys.stderr)
        print(f"\nDiagnosis: {diagnose(e.sip_status_code, e.sip_status, e)}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface anything else verbatim
        print("\nCALL FAILED (non-SIP error)", file=sys.stderr)
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        print(f"\nDiagnosis: {diagnose(None, None, e)}", file=sys.stderr)
        return 1
    else:
        print("ANSWERED -- the telephony path works.")
        print(f"  participant_id       : {participant.participant_id}")
        print(f"  participant_identity : {participant.participant_identity}")
        print(f"  room_name            : {participant.room_name}")
        print(f"  sip_call_id          : {participant.sip_call_id}")
        return 0
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    # asyncio.run executes main() exactly once. No loop, no retry.
    sys.exit(asyncio.run(main()))
