"""UTC timestamp helpers. All timestamps are fixed-width ISO-8601 strings so they sort correctly as text."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_FMT)


def now_iso() -> str:
    return to_iso(utcnow())


def parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_in(**delta) -> str:
    return to_iso(utcnow() + timedelta(**delta))


def iso_ago(**delta) -> str:
    return to_iso(utcnow() - timedelta(**delta))


def normalize(value: str | datetime | None) -> str | None:
    """Accept any ISO-8601 string/datetime from clients and store it in canonical form."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_iso(value)
    return to_iso(parse_iso(value))
