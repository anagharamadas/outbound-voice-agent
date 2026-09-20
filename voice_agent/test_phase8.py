#!/usr/bin/env python3
"""Phase 8 exit test: telephony wiring, WITHOUT placing a call.

Nothing here dials. Every outbound path is exercised against a fake LiveKit API
that records what it was asked to do, so the things that are easy to get wrong
and expensive to discover on a real call -- dispatching after dialling, reusing
a room, retrying a failed call, losing the patient id -- are asserted here for
free.

The real call is the human's exit test, and it is deliberately not automated:
it rings a real phone in India and bills by the rounded minute.

    ./venv/bin/python test_phase8.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DESTINATION_PHONE_NUMBER", "+10000000000")
os.environ.setdefault("OPIK_ENABLED", "false")
os.environ.setdefault("ANALYSIS_ENABLED", "false")

import dispatch
from src.agent import AGENT_NAME, patient_id_from_metadata
from src.patient import get_patient
from src.telephony import diagnose, mask

PATIENTS_FILE = Path(__file__).resolve().parent / "data" / "patients.json"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


class FakeSip:
    def __init__(self, fail_with=None):
        self.calls = []
        self._fail_with = fail_with

    async def create_sip_participant(self, req):
        self.calls.append(req)
        if self._fail_with:
            raise self._fail_with
        class P:
            participant_id = "PA_fake"
            sip_call_id = "SCL_fake"
            room_name = req.room_name
        return P()


class FakeDispatch:
    def __init__(self):
        self.dispatches = []

    async def create_dispatch(self, req):
        self.dispatches.append(req)
        return object()


class FakeLiveKitAPI:
    """Records the order of operations, which is the thing under test."""

    instances: list["FakeLiveKitAPI"] = []

    def __init__(self, sip_fail=None):
        self.sip = FakeSip(sip_fail)
        self.agent_dispatch = FakeDispatch()
        self.closed = False
        self.order: list[str] = []
        FakeLiveKitAPI.instances.append(self)
        # thread the order log through both fakes
        orig_d = self.agent_dispatch.create_dispatch
        orig_s = self.sip.create_sip_participant

        async def d(req):
            self.order.append("dispatch")
            return await orig_d(req)

        async def s(req):
            self.order.append("dial")
            return await orig_s(req)

        self.agent_dispatch.create_dispatch = d
        self.sip.create_sip_participant = s

    async def aclose(self):
        self.closed = True


def run() -> None:
    patient = get_patient(PATIENTS_FILE, "P001")
    config = dispatch.load_config()

    print("\n1. The agent is dispatched BEFORE the phone rings")
    FakeLiveKitAPI.instances.clear()
    dispatch.api.LiveKitAPI = lambda *a, **k: FakeLiveKitAPI()
    rc = asyncio.run(dispatch.place_call(patient, config))
    lk = FakeLiveKitAPI.instances[-1]
    check("exit code 0 on success", rc == 0)
    check("dispatch happened before the dial", lk.order == ["dispatch", "dial"], str(lk.order))
    check("exactly one dispatch", len(lk.agent_dispatch.dispatches) == 1)
    check("exactly one dial", len(lk.sip.calls) == 1)
    check("the API client is closed", lk.closed)

    print("\n2. The patient id travels as dispatch metadata")
    d = lk.agent_dispatch.dispatches[0]
    check("dispatch targets the named agent", d.agent_name == AGENT_NAME, d.agent_name)
    check("metadata carries the patient id",
          patient_id_from_metadata(d.metadata) == "P001", d.metadata)
    check("the agent can read it back",
          patient_id_from_metadata(d.metadata) == patient.patient_id)

    print("\n3. The dial targets the patient's own number, in the same room")
    call = lk.sip.calls[0]
    check("dials the patient's number", call.sip_call_to == patient.identity.phone_number,
          mask(call.sip_call_to))
    check("same room as the dispatch", call.room_name == d.room, f"{call.room_name} vs {d.room}")
    check("uses the configured trunk",
          call.sip_trunk_id == config.livekit_outbound_trunk_id)
    check("wait_until_answered is set", call.wait_until_answered is True,
          "so a failure surfaces here instead of as a silent empty room")

    print("\n4. Rooms are never reused between calls")
    names = {dispatch._room_name("P001") for _ in range(3)}
    check("room name includes the patient", all("P001" in n for n in names))
    import time
    a = dispatch._room_name("P001"); time.sleep(1.1); b = dispatch._room_name("P001")
    check("a later call gets a different room", a != b, f"{a} != {b}")

    print("\n5. A FAILED call is reported once and NEVER retried (hard rule 4)")
    class FakeSipError(Exception):
        sip_status_code = 403
        sip_status = "Forbidden"
    FakeLiveKitAPI.instances.clear()
    dispatch.api.SipCallError = FakeSipError
    dispatch.api.LiveKitAPI = lambda *a, **k: FakeLiveKitAPI(sip_fail=FakeSipError("denied"))
    rc = asyncio.run(dispatch.place_call(patient, config))
    lk = FakeLiveKitAPI.instances[-1]
    check("non-zero exit code", rc == 1)
    check("exactly ONE dial attempt", len(lk.sip.calls) == 1, f"{len(lk.sip.calls)} attempts")
    check("client still closed on the failure path", lk.closed)

    print("\n6. No retry construct exists anywhere in the dial path")
    src = Path("dispatch.py").read_text()
    body = src.split("async def place_call")[1].split("def main(")[0]
    for bad in ("while ", "for attempt", "retry", "range("):
        check(f"no {bad.strip()!r} in place_call", bad not in body)

    print("\n7. SIP failures name the layer at fault")
    check("403 blames the trunk/number association", "trunk" in diagnose(403, "Forbidden", "").lower())
    check("404 blames the number format", "E.164" in diagnose(404, "Not Found", ""))
    check("geo block is recognised", "Geo Permissions" in diagnose(None, None, "32205"))

    print("\n8. Without --call, nothing dials")
    FakeLiveKitAPI.instances.clear()
    rc = dispatch.main(["--patient-id", "P001"])
    check("exit code 0", rc == 0)
    check("no LiveKit client was even constructed", FakeLiveKitAPI.instances == [],
          "dry run must not touch the network")

    print("\n9. Narrowband readback, without weakening the gate")
    from src.prompts import unverified_instructions, verified_instructions
    u = unverified_instructions(patient.identity_payload())
    v = verified_instructions({"first_name": "Meera"}, patient.health.biomarkers)
    check("unverified prompt re-asks on an unclear name", "line is not very clear" in u)
    check("identifier readback is explicitly forbidden",
          "Never read back a date of" in u and "patient ID" in u)
    check("verified prompt reads the slot back before booking",
          "read the day and time back" in v)

    print("\n10. The prompt example leaks no real patient data")
    from src.patient import load_patients
    for pid, p in load_patients(PATIENTS_FILE).items():
        up = unverified_instructions(p.identity_payload())
        dob = p.verification.date_of_birth
        leaked = str(dob.year) in up and (
            str(dob.day) in up or dob.strftime("%B").lower() in up.lower())
        check(f"{pid}: date of birth absent from the unverified prompt", not leaked)
        check(f"{pid}: biomarker names absent",
              all(b.name.lower() not in up.lower() for b in p.health.biomarkers))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s")
    print(__doc__.strip().split("\n")[0])
    run()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All Phase 8 exit-test checks passed.")
