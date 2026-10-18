"""Sessions: opaque random token in an HttpOnly cookie, sha256 of it stored server-side.

Server-side sessions (rather than self-contained JWTs) so logout and revocation are immediate,
and so a user's team roles are always read fresh — a role change takes effect on the next request.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import config
from ..domain.policy import UserCtx
from ..errors import Unauthorized
from ..security import hash_token, new_session_token, verify_password
from ..timeutil import now_iso, to_iso, utcnow

SESSION_COOKIE = "opsdesk_session"


def load_user_ctx(conn: sqlite3.Connection, user_id: int) -> UserCtx:
    u = conn.execute("SELECT id, email, name, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if u is None:
        raise Unauthorized("User no longer exists")
    roles = {r["team_id"]: r["role"] for r in
             conn.execute("SELECT team_id, role FROM memberships WHERE user_id = ?", (user_id,))}
    return UserCtx(id=u["id"], email=u["email"], name=u["name"], is_admin=bool(u["is_admin"]), roles=roles)


def login(conn: sqlite3.Connection, email: str, password: str) -> tuple[str, UserCtx]:
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email.strip(),)).fetchone()
    # Same error for unknown email and wrong password: don't reveal which accounts exist.
    if row is None or not verify_password(password, row["password_hash"]):
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    token = new_session_token()
    expires = to_iso(utcnow() + timedelta(hours=config.settings.session_ttl_hours))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), row["id"], now_iso(), expires),
    )
    return token, load_user_ctx(conn, row["id"])


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def authenticate(conn: sqlite3.Connection, token: str | None) -> UserCtx:
    if not token:
        raise Unauthorized("Sign in required")
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?", (hash_token(token), now_iso())
    ).fetchone()
    if row is None:
        raise Unauthorized("Session expired; sign in again", code="session_expired")
    return load_user_ctx(conn, row["user_id"])
