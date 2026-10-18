"""Minimal forward-only migration runner: applies migrations/NNN_*.sql in order, once each.

Usage: python -m app.migrate
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config, db
from .timeutil import now_iso

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def migrate() -> list[str]:
    conn = db.get_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    newly_applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        # executescript manages its own transaction; wrap so a failing migration leaves no trace.
        try:
            conn.executescript(
                "BEGIN;\n" + sql
                + f"\nINSERT INTO schema_migrations VALUES ('{path.name}', '{now_iso()}');\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        newly_applied.append(path.name)
    return newly_applied


if __name__ == "__main__":
    done = migrate()
    print(f"database: {config.settings.database_path}")
    print("applied: " + (", ".join(done) if done else "nothing (up to date)"))
    sys.exit(0)
