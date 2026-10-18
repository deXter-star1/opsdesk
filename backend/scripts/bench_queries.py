"""Time the main read paths against the configured database.

    python -m app.seed --reset          # 40k items, ~170k events
    python scripts/bench_queries.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402
from app.services import auth, queries  # noqa: E402
from app.services.queries import ItemFilters  # noqa: E402

CASES = [
    ("default list (active, by activity)", dict()),
    ("all states", dict(state="all")),
    ("sort by priority", dict(sort="priority")),
    ("sort by due date", dict(sort="due")),
    ("full-text 'refund' (all states)", dict(q="refund", state="all")),
    ("full-text 'customer login'", dict(q="customer login")),
    ("view: unassigned P1/P2", dict(view="unassigned_urgent")),
    ("view: stale", dict(view="stale")),
]


def ms(fn):
    t = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t) * 1000


def main():
    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM work_items WHERE active = 1").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0]
    print(f"dataset: {total} items ({active} active), {events} events\n")
    for name in ("priya", "meera", "admin"):
        row = conn.execute("SELECT id FROM users WHERE email = ?", (f"{name}@opsdesk.test",)).fetchone()
        if row is None:
            continue
        user = auth.load_user_ctx(conn, row[0])
        print(f"user {name} (teams: {len(user.roles)})")
        for label, kw in CASES:
            f = ItemFilters(**kw)
            page1, t1 = ms(lambda: queries.list_items(conn, user, f))
            t2 = 0.0
            if page1["next_cursor"]:
                f.cursor = page1["next_cursor"]
                _, t2 = ms(lambda: queries.list_items(conn, user, f))
            print(f"  {label:38} page1 {t1:6.1f} ms   page2 {t2:6.1f} ms")
        _, td = ms(lambda: queries.dashboard(conn, user))
        print(f"  {'dashboard (6 tiles + activity)':38} {td:6.1f} ms\n")


if __name__ == "__main__":
    main()
