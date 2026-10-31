"""Helpers for calculating trading-day labels in the configured timezone."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _local_now(now: datetime | None, timezone_name: str) -> datetime:
    timezone = ZoneInfo(timezone_name)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def get_target_day(
    now: datetime | None = None, timezone_name: str = "Africa/Nairobi"
) -> str:
    """Return the next trading weekday after the previous local calendar day."""
    local_now = _local_now(now, timezone_name)
    candidate = local_now.date()
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.strftime("%A").upper()


def get_previous_trading_day(
    now: datetime | None = None, timezone_name: str = "Africa/Nairobi"
) -> str:
    """Return the most recent trading weekday before the local run date."""
    candidate = _local_now(now, timezone_name).date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.strftime("%A").upper()
