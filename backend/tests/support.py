"""Shared test fixtures: a fresh SQLite database per test, a small org, and a live HTTP server.

Tests run against a real database on purpose: the behaviours worth testing here (races, stale
writes, idempotency, job leases) live in the database interaction, and a mock would hide them.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid

from app import config, db
from app.migrate import migrate
from app.security import hash_password
from app.services import auth, items
from app.timeutil import now_iso

PASSWORD = "pw-test-123"
_PW_HASH = hash_password(PASSWORD)

# name -> {team_key: role}
ORG = {
    "pay_lead": {"PAY": "lead"},
    "pay_lead2": {"PAY": "lead"},
    "pay_member": {"PAY": "member"},
    "pay_member2": {"PAY": "member"},
    "pay_viewer": {"PAY": "viewer"},
    "sup_lead": {"SUP": "lead"},
    "sup_member": {"SUP": "member", "PAY": "viewer"},
    "outsider": {},
    "admin": {"SUP": "member"},
}


class DBTestCase(unittest.TestCase):
    """Fresh database + org for every test."""

    def setUp(self):
        # ignore_cleanup_errors: on Windows a file still open in another thread can't be deleted.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["OPSDESK_DB"] = os.path.join(self._tmp.name, "test.db")
        os.environ.setdefault("OPSDESK_REALTIME_POLL_SECONDS", "0.1")
        config.reload_settings()
        db.close_thread_conn()
        migrate()
        self.users: dict[str, int] = {}
        self.teams: dict[str, int] = {}
        conn = db.get_conn()
        with db.write_tx():
            for key, name in (("PAY", "Payments"), ("SUP", "Support")):
                self.teams[key] = conn.execute("INSERT INTO teams (key, name) VALUES (?, ?) RETURNING id",
                                               (key, name)).fetchone()["id"]
            for name, roles in ORG.items():
                uid = conn.execute(
                    "INSERT INTO users (email, name, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?) "
                    "RETURNING id", (f"{name}@test", name, _PW_HASH, int(name == "admin"), now_iso())
                ).fetchone()["id"]
                self.users[name] = uid
                for tkey, role in roles.items():
                    conn.execute("INSERT INTO memberships VALUES (?, ?, ?)", (self.teams[tkey], uid, role))

    def tearDown(self):
        db.close_thread_conn()
        self._tmp.cleanup()

    # -- helpers
    def ctx(self, name: str):
        return auth.load_user_ctx(db.get_conn(), self.users[name])

    def create(self, who: str = "pay_member", team: str = "PAY", **kw) -> dict:
        data = {"team_id": self.teams[team], "title": kw.pop("title", "Investigate duplicate payout"),
                "description": kw.pop("description", "details"), "kind": kw.pop("kind", "task"),
                "priority": kw.pop("priority", 3), "due_at": kw.pop("due_at", None),
                "requires_approval": kw.pop("requires_approval", None)}
        with db.write_tx() as conn:
            return items.create_item(conn, self.ctx(who), data)

    def run_cmd(self, fn, *args):
        """Run a service command in its own write transaction (like one HTTP request)."""
        with db.write_tx() as conn:
            return fn(conn, *args)

    def row(self, item_id: int) -> dict:
        return dict(db.get_conn().execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone())

    def count(self, sql: str, *params) -> int:
        return db.get_conn().execute(sql, params).fetchone()[0]

    def assertAppError(self, exc_type, code, fn, *args, **kwargs):
        with self.assertRaises(exc_type) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception


# --------------------------------------------------------------------------- live server

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self):
        import uvicorn
        from app.main import create_app
        self.port = _free_port()
        cfg = uvicorn.Config(create_app(), host="127.0.0.1", port=self.port, log_level="warning",
                             lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class Client:
    """Minimal HTTP client with a cookie jar of one cookie, like a browser tab."""

    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method: str, path: str, body=None, headers: dict | None = None, csrf: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        if csrf:
            h["X-Requested-With"] = "test"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and "opsdesk_session=" in set_cookie:
            self.cookie = set_cookie.split(";")[0]
        conn.close()
        data = json.loads(raw) if raw else None
        return resp.status, data, {k.lower(): v for k, v in resp.getheaders()}

    def login(self, name: str):
        status, data, _ = self.request("POST", "/api/auth/login", {"email": f"{name}@test", "password": PASSWORD})
        assert status == 200, data
        return self

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, key: str | None = None, **kw):
        headers = kw.pop("headers", {})
        if key:
            headers["Idempotency-Key"] = key
        return self.request("POST", path, body if body is not None else {}, headers=headers, **kw)

    def patch(self, path, body, **kw):
        return self.request("PATCH", path, body, **kw)


def new_key() -> str:
    return str(uuid.uuid4())
