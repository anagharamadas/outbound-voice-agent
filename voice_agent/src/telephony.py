"""SIP failure diagnosis, shared by the telephony spike and the dispatcher.

Lifted verbatim from `test_call.py`, which is where this mapping was established
and validated against a real Twilio trunk. Phase 8 task 3 asks the dispatcher to
report failures "with the layer diagnosis mapping already established in the
telephony spike" -- so it is shared rather than reimplemented, because two
copies would drift and the second one would be the untested one.

Nothing here places a call or holds a credential. It turns an upstream failure
into a sentence naming the layer most likely at fault, which on a trunk spanning
LiveKit, Twilio and an Indian carrier is most of the debugging effort.
"""

from __future__ import annotations


def mask(number: str) -> str:
    """Enough of the number to confirm it's the right one, not the whole thing."""
    if len(number) <= 7:
        return number
    return f"{number[:3]}{'*' * (len(number) - 7)}{number[-4:]}"


def diagnose(status_code, status_text, raw_error) -> str:
    """Map an upstream failure to the layer most likely responsible."""
    blob = f"{status_text or ''} {raw_error}".lower()

    # Twilio surfaces geo-permission blocks as notification 32205 rather than as
    # a distinct SIP status, so match it on the message text.
    if "32205" in blob:
        return (
            "Twilio Geo Permissions -- India is not enabled on the account.\n"
            "  Fix: Twilio Console > Voice > Settings > Geographic Permissions, enable India (IN)."
        )

    if status_code is None:
        return (
            "No SIP response from the provider -- transport or address problem.\n"
            "  Check TWILIO_SIP_TERMINATION_DOMAIN is a bare hostname with no 'sip:' prefix,\n"
            "  and that the trunk's termination domain actually exists in Twilio."
        )

    if status_code == 401:
        return (
            "Credential mismatch.\n"
            "  The username/password on the LiveKit trunk does not match the Twilio\n"
            "  termination credential list. Re-check TWILIO_SIP_AUTH_USERNAME / _PASSWORD."
        )

    if status_code == 403:
        return (
            "Trunk auth rejected, or the US number is not associated with the Twilio trunk.\n"
            "  Check: the credential list is attached to the trunk's Termination settings,\n"
            "  and TWILIO_PHONE_NUMBER is listed under the trunk's Numbers."
        )

    if status_code == 404:
        return (
            "Number not found -- almost always a format problem.\n"
            "  DESTINATION_PHONE_NUMBER must be full E.164 with a leading + (e.g. +91XXXXXXXXXX)."
        )

    if status_code in (480, 486, 487, 603):
        return (
            f"Call reached the network but was not answered (SIP {status_code}).\n"
            "  If the phone rang and then dropped: likely a trial account restriction,\n"
            "  or region pinning not applied (destination_country on the trunk).\n"
            "  If it never rang: the carrier may be filtering the toll-free caller ID."
        )

    if 500 <= status_code < 600:
        return (
            f"Upstream SIP/trunk failure (SIP {status_code}).\n"
            "  Usually a trunk configuration problem on the Twilio side."
        )

    return (
        f"Unmapped SIP status {status_code}.\n"
        "  If the phone rang and then dropped, suspect a trial account restriction\n"
        "  or region pinning not applied."
    )
