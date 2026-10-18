"""Seed a realistic dataset.

    python -m app.seed --reset                 # 40k items, ~2k users, ~200k history events
    python -m app.seed --reset --items 2000    # quick

Bulk history is inserted directly (fast, and consistent: versions match their events, overdue
items already carry their SLA flag so the first sweep doesn't flood anyone). A handful of
curated demo scenarios are then created through the real service layer, and the job queue is
drained so demo users start with notifications.
"""
from __future__ import annotations

import argparse
import os
import random
import time
from datetime import timedelta
from pathlib import Path

from . import config, db
from .domain.policy import UserCtx
from .migrate import migrate
from .security import hash_password
from .services import auth as auth_service
from .services import items as item_service
from .timeutil import to_iso, utcnow
from .worker import run_once

DEMO_PASSWORD = "demo1234"

TEAMS = [
    ("SUP", "Customer Support", "Customer-facing issues and escalations"),
    ("ENG", "Engineering", "Product engineering and bug fixes"),
    ("PAY", "Payments", "Payment investigations, refunds and reconciliation"),
    ("SRE", "Site Reliability", "Production incidents and infrastructure"),
    ("CMP", "Compliance", "Regulatory, audit and data requests"),
    ("OPS", "Business Operations", "Internal operational tasks needing coordination"),
]

# email, name, is_admin, {team_key: role}
DEMO_USERS = [
    ("admin@opsdesk.test", "Asha Admin", True, {"OPS": "lead", "SRE": "viewer"}),
    ("priya@opsdesk.test", "Priya Sharma", False, {"SUP": "lead", "ENG": "member"}),
    ("rahul@opsdesk.test", "Rahul Verma", False, {"SUP": "member"}),
    ("meera@opsdesk.test", "Meera Iyer", False, {"PAY": "lead"}),
    ("arjun@opsdesk.test", "Arjun Nair", False, {"PAY": "member", "SUP": "member"}),
    ("kabir@opsdesk.test", "Kabir Singh", False, {"ENG": "lead", "SRE": "member"}),
    ("neha@opsdesk.test", "Neha Kulkarni", False, {"SRE": "lead", "ENG": "member"}),
    ("vikram@opsdesk.test", "Vikram Rao", False, {"CMP": "lead", "PAY": "viewer"}),
    ("isha@opsdesk.test", "Isha Patel", False, {"SUP": "viewer", "CMP": "member"}),
    ("sam@opsdesk.test", "Sam Thomas", False, {}),  # belongs to no team: can only file & follow requests
]

TEAM_KINDS = {
    "SUP": ["customer_issue"] * 8 + ["task", "engineering"],
    "ENG": ["engineering"] * 8 + ["incident", "task"],
    "PAY": ["payment"] * 8 + ["customer_issue", "compliance"],
    "SRE": ["incident"] * 7 + ["engineering"] * 3,
    "CMP": ["compliance"] * 8 + ["task", "payment"],
    "OPS": ["task"] * 8 + ["compliance", "customer_issue"],
}

TITLES = {
    "customer_issue": ["Customer cannot log in after password reset", "Order #{n} shows delivered but not received",
                       "Enterprise client {c} reports missing invoices", "Duplicate charge complaint from {c}",
                       "Account locked for {c} admin user", "Export to CSV failing for {c}",
                       "Escalation: {c} threatening to churn over response times"],
    "engineering": ["Search results ignore team filter on mobile", "Memory leak in report generator service",
                    "Flaky integration test in billing pipeline", "Upgrade TLS library on API gateway",
                    "Notification emails render badly in Outlook", "Timezone bug in scheduled exports",
                    "Rate limiter misconfigured for partner API {c}"],
    "payment": ["Refund of INR {amt} for order #{n} failed", "Chargeback dispute from {c}",
                "Settlement mismatch of INR {amt} on {d}", "Payout to vendor {c} stuck in pending",
                "Duplicate payout INR {amt} to {c}", "Investigate failed UPI mandates for {c}"],
    "incident": ["Elevated 5xx on checkout API", "Database replica lag above 30s", "Queue backlog on email workers",
                 "Latency spike in ap-south-1", "Certificate expiring on {c} integration endpoint",
                 "Disk usage 90% on log cluster", "Payment gateway timeouts above SLO"],
    "compliance": ["Data deletion request from {c}", "Quarterly access review for finance systems",
                   "KYC re-verification batch for {c}", "Audit evidence request: change management",
                   "Vendor security questionnaire for {c}", "Retention policy exception for {c}"],
    "task": ["Onboard new support hires to tooling", "Renew SaaS contract with {c}", "Update on-call rotation for Q4",
             "Prepare monthly ops metrics review", "Approve laptop purchase for {c} team",
             "Coordinate office network maintenance window"],
}
COMPANIES = ["Acme Retail", "Zenith Foods", "Bluefin Logistics", "Northwind", "Kestrel Travel", "Orbit Media",
             "Saffron Hotels", "Lumen Health", "Tata Agro", "Pioneer Motors", "Helix Labs", "Monsoon Apparel"]
DESCRIPTIONS = [
    "Reported via chat by the account manager. Customer impact confirmed; screenshots attached in the thread.",
    "First noticed by the overnight shift. Needs investigation before the next business day.",
    "Raised in the weekly review. Previous attempt to fix this was rolled back; see linked history.",
    "Customer has contacted us three times about this. Please keep the reporter updated.",
    "Low urgency but has been forgotten twice in Slack threads. Tracking it here so it does not get lost.",
    "Requires sign-off from a lead before any money moves or data is changed.",
]
COMMENTS = ["Looking into this now.", "Reproduced on staging.", "Waiting on the customer for more details.",
            "Shared logs with the vendor.", "This looks related to last week's deploy.",
            "Customer confirmed the workaround helps.", "Escalated internally, will update by EOD.",
            "Added the reconciliation sheet to the thread."]


def _title(kind: str, rnd: random.Random) -> str:
    t = rnd.choice(TITLES[kind])
    return t.format(n=rnd.randint(10000, 99999), c=rnd.choice(COMPANIES), amt=f"{rnd.randint(5, 900) * 100:,}",
                    d=f"{rnd.randint(1, 28)} Sep")


def seed(n_items: int = 40000, n_users: int = 2000, seed_value: int = 7, verbose: bool = True) -> dict:
    rnd = random.Random(seed_value)
    t0 = time.time()
    migrate()
    conn = db.get_conn()
    now = utcnow()
    now_s = to_iso(now)

    with db.write_tx():
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]:
            raise SystemExit("Database already has data. Use --reset to recreate it.")
        team_ids = {}
        for key, name, desc in TEAMS:
            team_ids[key] = conn.execute("INSERT INTO teams (key, name, description) VALUES (?, ?, ?) RETURNING id",
                                         (key, name, desc)).fetchone()["id"]

        pw = hash_password(DEMO_PASSWORD)  # one hash reused for all seeded accounts (seed speed)
        members_by_team: dict[int, list[int]] = {t: [] for t in team_ids.values()}  # member+lead (assignable)
        leads_by_team: dict[int, list[int]] = {t: [] for t in team_ids.values()}
        all_users: list[int] = []
        demo_ids = {}
        for email, name, is_admin, roles in DEMO_USERS:
            uid = conn.execute("INSERT INTO users (email, name, password_hash, is_admin, created_at) "
                               "VALUES (?, ?, ?, ?, ?) RETURNING id", (email, name, pw, int(is_admin), now_s)
                               ).fetchone()["id"]
            demo_ids[email.split("@")[0]] = uid
            all_users.append(uid)
            for tkey, role in roles.items():
                tid = team_ids[tkey]
                conn.execute("INSERT INTO memberships VALUES (?, ?, ?)", (tid, uid, role))
                if role in ("member", "lead"):
                    members_by_team[tid].append(uid)
                if role == "lead":
                    leads_by_team[tid].append(uid)
        first_names = ["Aarav", "Diya", "Kiran", "Ananya", "Rohan", "Sneha", "Vivaan", "Pooja", "Aditya", "Riya",
                       "Ishaan", "Kavya", "Manav", "Tara", "Nikhil", "Zara", "Yash", "Mira", "Dev", "Leela"]
        last_names = ["Shah", "Menon", "Gupta", "Reddy", "Joshi", "Das", "Bose", "Khan", "Pillai", "Mehta",
                      "Chopra", "Ghosh", "Bhat", "Sinha", "Kapoor"]
        user_rows, membership_rows = [], []
        next_uid = conn.execute("SELECT MAX(id) AS m FROM users").fetchone()["m"] + 1
        tid_list = list(team_ids.values())
        for i in range(n_users):
            uid = next_uid + i
            user_rows.append((uid, f"user{i:05d}@opsdesk.test",
                              f"{rnd.choice(first_names)} {rnd.choice(last_names)}", pw, 0, now_s))
            for tid in rnd.sample(tid_list, k=rnd.choice([1, 1, 1, 2])):
                role = rnd.choices(["viewer", "member", "lead"], weights=[20, 77, 3])[0]
                membership_rows.append((tid, uid, role))
                if role != "viewer":
                    members_by_team[tid].append(uid)
                if role == "lead":
                    leads_by_team[tid].append(uid)
            all_users.append(uid)
        conn.executemany("INSERT INTO users (id, email, name, password_hash, is_admin, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", user_rows)
        conn.executemany("INSERT INTO memberships VALUES (?, ?, ?)", membership_rows)

        # ---- bulk items with consistent histories
        team_keys = [k for k, _, _ in TEAMS]
        specs = []
        for _ in range(n_items):
            age_days = rnd.random() ** 2 * 180  # skew towards recent
            created = now - timedelta(days=age_days, minutes=rnd.randint(0, 600))
            specs.append((created, rnd.choice(team_keys)))
        specs.sort(key=lambda s: s[0])
        item_rows, event_rows, watcher_rows = [], [], []
        for item_id, (created, tkey) in enumerate(specs, start=1):
            tid = team_ids[tkey]
            kind = rnd.choice(TEAM_KINDS[tkey])
            priority = rnd.choices([1, 2, 3, 4], weights=[5, 20, 50, 25])[0]
            requires_approval = kind in ("payment", "compliance") and rnd.random() < 0.8
            age_days = (now - created).total_seconds() / 86400
            p_closed = min(0.92, 0.15 + age_days / 40)
            if rnd.random() < p_closed:
                status = rnd.choices(["resolved", "cancelled"], weights=[88, 12])[0]
            else:
                status = rnd.choices(["open", "in_progress", "blocked", "awaiting_approval", "approved"],
                                     weights=[30, 45, 10, 8, 7])[0]
                if status in ("awaiting_approval", "approved") and not requires_approval:
                    status = "in_progress"
            reporter = rnd.choice(members_by_team[tid] if rnd.random() < 0.85 else all_users)
            assignee = None
            if status != "open" or rnd.random() < 0.3:
                pool = members_by_team[tid]
                assignee = rnd.choice(pool) if pool else None
                if assignee is None and status != "cancelled":
                    status = "open"
            if status == "cancelled" and rnd.random() < 0.5:
                assignee = None
            lead_pool = [u for u in leads_by_team[tid] if u != assignee] or [reporter]

            t = created
            version = 0
            evs = []  # (created_at, actor, kind, versioned, data)

            def step(minutes_max: int) -> None:
                # Advance the item's clock, never past "now" and compressing for recent items.
                nonlocal t
                remaining = (now - t).total_seconds() / 60
                if remaining > 1:
                    t = t + timedelta(minutes=min(rnd.randint(5, minutes_max), remaining * rnd.uniform(0.1, 0.5)))

            version += 1
            evs.append((t, reporter, "created", version, {"title": None, "priority": priority, "kind": kind}))
            approval_requested_by = approved_by = approved_at = resolution = resolved_at = None
            if assignee is not None:
                step(240)
                version += 1
                via = "claim" if status != "open" else "assign"
                actor = assignee if via == "claim" else rnd.choice(lead_pool)
                to_status = "in_progress" if via == "claim" else "open"
                evs.append((t, actor, "assigned", version, {"from": None, "to": assignee, "via": via,
                                                            "status": ["open", to_status]}))
            for _ in range(rnd.choice([0, 0, 1, 1, 2, 3])):
                step(600)
                evs.append((t, rnd.choice([reporter, assignee or reporter]), "commented", None,
                            {"body": rnd.choice(COMMENTS)}))
            if status == "blocked":
                step(600)
                version += 1
                evs.append((t, assignee, "status_changed", version,
                            {"action": "block", "from": "in_progress", "to": "blocked",
                             "note": "Waiting on an external party"}))
            needs_approval_path = status in ("awaiting_approval", "approved") or (
                status == "resolved" and requires_approval)
            if needs_approval_path:
                step(900)
                version += 1
                approval_requested_by = assignee
                evs.append((t, assignee, "status_changed", version,
                            {"action": "submit_for_approval", "from": "in_progress", "to": "awaiting_approval",
                             "note": None}))
                if status in ("approved", "resolved"):
                    step(900)
                    version += 1
                    approved_by = rnd.choice(lead_pool)
                    approved_at = to_iso(t)
                    evs.append((t, approved_by, "status_changed", version,
                                {"action": "approve", "from": "awaiting_approval", "to": "approved", "note": None,
                                 "requested_by": assignee}))
            if status in ("resolved", "cancelled"):
                step(2000)
                version += 1
                resolution = "Fixed and verified with the reporter." if status == "resolved" else "Duplicate request."
                actor = assignee if (status == "resolved" and assignee) else rnd.choice(lead_pool)
                prev = ("approved" if needs_approval_path else "in_progress") if status == "resolved" else (
                    "in_progress" if assignee else "open")
                evs.append((t, actor, "status_changed", version,
                            {"action": "resolve" if status == "resolved" else "cancel", "from": prev,
                             "to": status, "note": resolution}))
                resolved_at = to_iso(t)
            sla_h = config.settings.sla_hours_by_priority[priority]
            active = status not in ("resolved", "cancelled")
            if not active:
                due = created + timedelta(hours=sla_h * rnd.choice([1, 1, 2, 4]))
            elif rnd.random() < 0.12:  # a realistic minority of open work is late
                due = now - timedelta(hours=rnd.uniform(1, 96))
            else:
                due = max(created, now) + timedelta(hours=rnd.uniform(1, sla_h * 2))
            if active and (now - max(e[0] for e in evs)).days >= 3 and rnd.random() < 0.93:
                # Most long-running work still gets touched; the rest shows up as "going stale".
                evs.append((now - timedelta(hours=rnd.uniform(0.2, 60)), assignee or reporter, "commented", None,
                            {"body": rnd.choice(COMMENTS)}))
            sla_breached_at = None
            if active and due < now:
                sla_breached_at = to_iso(due)
                evs.append((due, None, "sla_breached", None, {"due_at": to_iso(due)}))
            title = _title(kind, rnd)
            evs[0][4]["title"] = title
            last_versioned = max(e[0] for e in evs if e[3] is not None)
            last_activity = max(e[0] for e in evs)
            item_rows.append((item_id, tid, title, rnd.choice(DESCRIPTIONS), kind, priority, status, assignee,
                              reporter, int(requires_approval), approval_requested_by, approved_by, approved_at,
                              to_iso(due), sla_breached_at, resolution, version, to_iso(created),
                              to_iso(last_versioned), to_iso(last_activity), resolved_at))
            for (ts, actor, kind_, ver, data) in evs:
                event_rows.append((ts, item_id, tid, actor, kind_, ver, db.dumps(data)))
            watcher_rows.append((item_id, reporter, to_iso(created)))
            if assignee and assignee != reporter:
                watcher_rows.append((item_id, assignee, to_iso(created)))

        conn.executemany(
            """INSERT INTO work_items (id, team_id, title, description, kind, priority, status, assignee_id,
                   reporter_id, requires_approval, approval_requested_by, approved_by, approved_at, due_at,
                   sla_breached_at, resolution, version, created_at, updated_at, last_activity_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", item_rows)
        event_rows.sort(key=lambda e: e[0])  # event ids in time order, so "recent activity" is truly recent
        conn.executemany(
            "INSERT INTO item_events (item_id, team_id, actor_id, kind, item_version, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(e[1], e[2], e[3], e[4], e[5], e[6], to_iso(e[0])) for e in event_rows])
        conn.executemany("INSERT INTO item_watchers VALUES (?, ?, ?) ON CONFLICT DO NOTHING", watcher_rows)

    conn.execute("ANALYZE")
    stats = {"teams": len(TEAMS), "users": len(all_users), "items": len(item_rows), "events": len(event_rows)}
    if verbose:
        print(f"bulk data: {stats} in {time.time() - t0:.1f}s")

    _curated_scenarios(conn, demo_ids, team_ids)
    drained = 0
    while run_once("seed", batch=200):
        drained += 1
    if verbose:
        print(f"curated scenarios created; job queue drained; total {time.time() - t0:.1f}s")
    return stats


def _ctx(conn, uid: int) -> UserCtx:
    return auth_service.load_user_ctx(conn, uid)


def _curated_scenarios(conn, demo: dict, teams: dict) -> None:
    """A few recent, recognisable items that exercise the interesting paths in a demo."""
    def as_user(name):
        return _ctx(conn, demo[name])

    with db.write_tx():
        # 1. Payment needing approval, assigned to Arjun and submitted: Meera (PAY lead) must approve.
        it = item_service.create_item(conn, as_user("arjun"), {
            "team_id": teams["PAY"], "title": "Refund INR 48,000 to Zenith Foods for duplicate settlement",
            "description": "Settlement on 14 Oct was processed twice. Finance confirmed the duplicate. "
                           "Refund must be approved by a Payments lead before it is executed.",
            "kind": "payment", "priority": 2, "due_at": None, "requires_approval": True})
        item_service.claim(conn, as_user("arjun"), it["id"])
        item_service.transition(conn, as_user("arjun"), it["id"], "submit_for_approval", None, None)
        item_service.add_comment(conn, as_user("arjun"), it["id"],
                                 "Bank reference and both settlement IDs are in the attached sheet.")

        # 2. Unowned P1 incident in SRE: good for the "two people claim at once" demo.
        item_service.create_item(conn, as_user("neha"), {
            "team_id": teams["SRE"], "title": "Checkout API returning 502 for 8% of requests",
            "description": "Alert fired at 09:12. Error rate climbing since the 09:00 deploy. Needs an owner now.",
            "kind": "incident", "priority": 1, "due_at": None, "requires_approval": None})

        # 3. Request filed by Sam (no team) into Support: Sam can follow and comment, not edit.
        it = item_service.create_item(conn, as_user("sam"), {
            "team_id": teams["SUP"], "title": "Kestrel Travel cannot download October invoices",
            "description": "Their finance team needs the invoices for month-end close on Friday.",
            "kind": "customer_issue", "priority": 2, "due_at": None, "requires_approval": None})
        item_service.assign(conn, as_user("priya"), it["id"], demo["rahul"], None)

        # 4. Rahul's in-progress item that was handed between teams.
        it = item_service.create_item(conn, as_user("rahul"), {
            "team_id": teams["SUP"], "title": "Search ignores team filter on mobile web",
            "description": "Several agents report the team filter resets on mobile. Looks like a frontend bug.",
            "kind": "engineering", "priority": 3, "due_at": None, "requires_approval": None})
        item_service.move(conn, as_user("rahul"), it["id"], teams["ENG"], None)
        item_service.assign(conn, as_user("kabir"), it["id"], demo["neha"], None)
        item_service.transition(conn, as_user("neha"), it["id"], "start", None, None)

        # 5. Compliance deletion request awaiting Vikram's approval, assigned to Isha.
        it = item_service.create_item(conn, as_user("isha"), {
            "team_id": teams["CMP"], "title": "GDPR deletion request from Orbit Media user",
            "description": "Verified identity via support ticket. Deletion across CRM and analytics required.",
            "kind": "compliance", "priority": 2, "due_at": None, "requires_approval": None})
        item_service.claim(conn, as_user("isha"), it["id"])
        item_service.transition(conn, as_user("isha"), it["id"], "submit_for_approval", None, None)


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the OpsDesk database")
    ap.add_argument("--items", type=int, default=int(os.environ.get("SEED_ITEMS", 40000)))
    ap.add_argument("--users", type=int, default=int(os.environ.get("SEED_USERS", 2000)))
    ap.add_argument("--reset", action="store_true", help="delete the database file first")
    args = ap.parse_args()
    path = Path(config.settings.database_path)
    if args.reset:
        db.close_thread_conn()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    print(f"seeding {path} ...")
    seed(args.items, args.users)
    print(f"done. Sign in with any demo account, password: {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
