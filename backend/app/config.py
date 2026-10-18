"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_path: str
    static_dir: str
    session_ttl_hours: int
    cookie_secure: bool
    # Realtime: how often each API process tails the event log for new events.
    realtime_poll_seconds: float
    # Worker
    worker_poll_seconds: float
    job_lease_seconds: int
    # Fault injection for demos: probability that a job handler raises before doing work.
    job_failure_rate: float
    # SLA defaults (hours) applied when an item is created without an explicit due date.
    sla_hours_by_priority: dict
    stale_after_hours: int


def load_settings() -> Settings:
    return Settings(
        database_path=os.environ.get("OPSDESK_DB", str(BACKEND_DIR / "data" / "opsdesk.db")),
        static_dir=os.environ.get("OPSDESK_STATIC_DIR", str(REPO_DIR / "frontend" / "dist")),
        session_ttl_hours=_int("OPSDESK_SESSION_TTL_HOURS", 24 * 7),
        cookie_secure=os.environ.get("OPSDESK_COOKIE_SECURE", "0") == "1",
        realtime_poll_seconds=_float("OPSDESK_REALTIME_POLL_SECONDS", 1.0),
        worker_poll_seconds=_float("OPSDESK_WORKER_POLL_SECONDS", 0.5),
        job_lease_seconds=_int("OPSDESK_JOB_LEASE_SECONDS", 30),
        job_failure_rate=_float("OPSDESK_JOB_FAILURE_RATE", 0.0),
        sla_hours_by_priority={1: 4, 2: 24, 3: 72, 4: 168},
        stale_after_hours=_int("OPSDESK_STALE_AFTER_HOURS", 72),
    )


settings = load_settings()


def reload_settings() -> Settings:
    """Used by tests after changing environment variables."""
    global settings
    settings = load_settings()
    return settings
