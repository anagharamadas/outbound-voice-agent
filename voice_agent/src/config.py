"""Environment loading and validation.

Fails fast, naming the variable that is missing. Nothing here reads or
holds patient data -- see src/patient.py for that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Variables the Python application needs. The Twilio SIP auth credentials are
# deliberately absent: they are consumed by create_trunk.sh when provisioning
# the LiveKit trunk, never by this process.
REQUIRED_VARS: tuple[str, ...] = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "LIVEKIT_OUTBOUND_TRUNK_ID",
    "TWILIO_PHONE_NUMBER",
    "DESTINATION_PHONE_NUMBER",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when the environment is not usable. Message names the variable."""


@dataclass(frozen=True)
class Config:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_outbound_trunk_id: str
    twilio_phone_number: str
    destination_phone_number: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let a Config land in a log or traceback with the secret in it.
        return (
            f"Config(livekit_url={self.livekit_url!r}, "
            f"livekit_api_key=<redacted>, livekit_api_secret=<redacted>, "
            f"livekit_outbound_trunk_id={self.livekit_outbound_trunk_id!r}, "
            f"twilio_phone_number=<redacted>, destination_phone_number=<redacted>)"
        )


def load_config(*, required: tuple[str, ...] = REQUIRED_VARS) -> Config:
    """Load .env and return a validated Config.

    Raises ConfigError naming every missing variable, rather than failing
    later at the point of use with something less legible.
    """
    load_dotenv(PROJECT_ROOT / ".env")

    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        raise ConfigError(
            "missing or empty in .env: "
            + ", ".join(missing)
            + f"\nExpected a .env file at {PROJECT_ROOT / '.env'}. "
            "See .env.example for the full list."
        )

    def get(name: str) -> str:
        return (os.getenv(name) or "").strip()

    return Config(
        livekit_url=get("LIVEKIT_URL"),
        livekit_api_key=get("LIVEKIT_API_KEY"),
        livekit_api_secret=get("LIVEKIT_API_SECRET"),
        livekit_outbound_trunk_id=get("LIVEKIT_OUTBOUND_TRUNK_ID"),
        twilio_phone_number=get("TWILIO_PHONE_NUMBER"),
        destination_phone_number=get("DESTINATION_PHONE_NUMBER"),
    )
