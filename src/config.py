"""Application configuration loaded from the environment and .env file."""

from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    email: str
    password: str
    timezone: str
    login_url: str
    portal_url: str
    headless: bool
    action_timeout_ms: int

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        load_dotenv(project_root / ".env")

        email = os.getenv("TRADEOPS_EMAIL", "").strip()
        password = os.getenv("TRADEOPS_PASSWORD", "")
        if not email or not password:
            raise ValueError("TRADEOPS_EMAIL and TRADEOPS_PASSWORD must be set in .env")

        timezone = os.getenv("TIMEZONE", "Africa/Nairobi").strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone}") from exc

        timeout_raw = os.getenv("ACTION_TIMEOUT_MS", "15000").strip()
        try:
            action_timeout_ms = int(timeout_raw)
        except ValueError as exc:
            raise ValueError("ACTION_TIMEOUT_MS must be a positive integer") from exc
        if action_timeout_ms <= 0:
            raise ValueError("ACTION_TIMEOUT_MS must be a positive integer")

        return cls(
            email=email,
            password=password,
            timezone=timezone,
            login_url=os.getenv(
                "TRADEOPS_LOGIN_URL", "https://www.tradeopss.com/login"
            ).strip(),
            portal_url=os.getenv(
                "TRADEOPS_PORTAL_URL",
                "https://www.tradeopss.com/trader/Gideon%20Oruma",
            ).strip(),
            headless=os.getenv("HEADLESS", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            action_timeout_ms=action_timeout_ms,
        )
