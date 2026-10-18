"""Live updates over Server-Sent Events, fed by tailing the append-only event log.

Each API process runs ONE background task that polls `item_events` / `notifications` for
rows with an id above its cursor (one cheap indexed query per second, regardless of how
many browsers are connected) and fans them out to that process's subscribers.

Design choices
  * Events are hints, not data. A pushed event tells the browser "item 42 is now at version 7";
    the browser then re-fetches through the normal, authorized API. So the stream never has
    to be the source of truth, and a dropped event costs freshness, not correctness.
  * Authorization applies to the push channel too: a subscriber only receives events for
    teams it belongs to or items it reported, and only its own notifications.
  * Resume: every event carries its log id as the SSE `id:`. On reconnect the browser sends
    Last-Event-ID and we replay what it missed (up to REPLAY_LIMIT); beyond that we tell it
    to `resync` (refetch everything) instead of streaming an unbounded backlog.
  * Slow consumers: each subscriber has a bounded queue. If it overflows we drop its backlog
    and send `resync` rather than buffering without limit.

Portability note: tailing by id is gap-free on SQLite because commits are serialized in id
order. On PostgreSQL ids can commit out of order (txn A takes id 10, txn B takes 11 and commits
first), so there we would wake on LISTEN/NOTIFY and re-read a short trailing window, or tail
by commit order — see ENGINEERING_DECISIONS.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import StreamingResponse

from . import config, db
from .db import loads
from .services.auth import SESSION_COOKIE, authenticate

log = logging.getLogger("opsdesk.realtime")

REPLAY_LIMIT = 500
QUEUE_SIZE = 1000
HEARTBEAT_SECONDS = 15

_EVENT_SQL = """
    SELECT e.id, e.item_id, e.team_id, e.kind, e.item_version, e.actor_id, e.data, e.created_at,
           u.name AS actor_name, w.reporter_id
    FROM item_events e
    JOIN work_items w ON w.id = e.item_id
    LEFT JOIN users u ON u.id = e.actor_id
    WHERE e.id > ? {extra}
    ORDER BY e.id LIMIT ?
"""


def visible_to(user_id: int, team_ids: frozenset, event: dict) -> bool:
    """Same visibility rule as policy.can_view, applied to stream events."""
    return event["team_id"] in team_ids or event["reporter_id"] == user_id


def event_message(row: dict) -> dict:
    data = loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    msg = {
        "id": row["id"], "item_id": row["item_id"], "team_id": row["team_id"], "kind": row["kind"],
        "item_version": row["item_version"], "actor_id": row["actor_id"], "actor_name": row["actor_name"],
        "created_at": row["created_at"],
    }
    # Small, non-sensitive summary so the UI can say *what* changed without refetching first.
    if row["kind"] == "updated":
        msg["fields"] = sorted(data.get("changes", {}))
    elif row["kind"] == "status_changed":
        msg["to"] = data.get("to")
    elif row["kind"] == "assigned":
        msg["to_name"] = data.get("to_name")
    return msg


@dataclass(eq=False)
class Subscriber:
    user_id: int
    team_ids: frozenset
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    overflowed: bool = False

    def offer(self, item) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True


class Broker:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        self.event_cursor = 0
        self.notification_cursor = 0
        self._task: asyncio.Task | None = None

    # -- lifecycle
    async def start(self) -> None:
        self.event_cursor, self.notification_cursor = await run_in_threadpool(self._current_cursors)
        self._task = asyncio.create_task(self._run(), name="realtime-broker")

    async def stop(self) -> None:
        for sub in list(self.subscribers):
            try:
                sub.queue.put_nowait(None)  # tells the stream generator to finish
            except asyncio.QueueFull:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _current_cursors() -> tuple[int, int]:
        conn = db.get_conn()
        e = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM item_events").fetchone()["m"]
        n = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM notifications").fetchone()["m"]
        return e, n

    # -- polling loop
    def _poll(self) -> tuple[list[dict], list[dict]]:
        with db.read_tx() as conn:
            evs = [dict(r) for r in conn.execute(_EVENT_SQL.format(extra=""), (self.event_cursor, 500))]
            notes = [dict(r) for r in conn.execute(
                "SELECT id, user_id, item_id, reason FROM notifications WHERE id > ? ORDER BY id LIMIT 500",
                (self.notification_cursor,))]
        return evs, notes

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(config.settings.realtime_poll_seconds)
            try:
                evs, notes = await run_in_threadpool(self._poll)
            except Exception:
                log.exception("realtime poll failed")
                continue
            if evs:
                self.event_cursor = evs[-1]["id"]
            if notes:
                self.notification_cursor = notes[-1]["id"]
            self.dispatch(evs, notes)

    def dispatch(self, evs: list[dict], notes: list[dict]) -> None:
        for sub in list(self.subscribers):
            for ev in evs:
                if visible_to(sub.user_id, sub.team_ids, ev):
                    sub.offer(("item", ev["id"], event_message(ev)))
            for n in notes:
                if n["user_id"] == sub.user_id:
                    sub.offer(("notification", None, {"id": n["id"], "item_id": n["item_id"],
                                                      "reason": n["reason"]}))

    # -- subscriptions
    def subscribe(self, user_id: int, team_ids) -> tuple[Subscriber, int]:
        sub = Subscriber(user_id=user_id, team_ids=frozenset(team_ids))
        self.subscribers.add(sub)
        return sub, self.event_cursor  # events after this cursor will arrive through the queue

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)


broker = Broker()


def _replay(user_id: int, team_ids: frozenset, after_id: int, up_to: int) -> list[dict] | None:
    """Events the client missed while disconnected; None if the gap is too large to replay."""
    if up_to - after_id > REPLAY_LIMIT:
        return None
    conn = db.get_conn()
    rows = [dict(r) for r in conn.execute(_EVENT_SQL.format(extra="AND e.id <= ?"),
                                          (after_id, up_to, REPLAY_LIMIT))]
    return [event_message(r) for r in rows if visible_to(user_id, team_ids, r)]


def _sse(event: str, data: dict, event_id: int | None = None) -> str:
    out = f"event: {event}\n"
    if event_id is not None:
        out += f"id: {event_id}\n"
    return out + f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_endpoint(request: Request):
    def auth():
        return authenticate(db.get_conn(), request.cookies.get(SESSION_COOKIE))

    user = await run_in_threadpool(auth)
    last_id_raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    last_id = int(last_id_raw) if last_id_raw and last_id_raw.isdigit() else None
    sub, start_cursor = broker.subscribe(user.id, user.roles.keys())

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield _sse("hello", {"cursor": start_cursor, "user_id": user.id})
            if last_id is not None and last_id < start_cursor:
                missed = await run_in_threadpool(_replay, user.id, sub.team_ids, last_id, start_cursor)
                if missed is None:
                    yield _sse("resync", {"reason": "too_far_behind"})
                else:
                    for m in missed:
                        yield _sse("item", m, m["id"])
            while True:
                if sub.overflowed:
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    sub.overflowed = False
                    yield _sse("resync", {"reason": "slow_consumer"})
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if msg is None:  # server shutting down
                    return
                kind, event_id, payload = msg
                yield _sse(kind, payload, event_id)
        finally:
            broker.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
