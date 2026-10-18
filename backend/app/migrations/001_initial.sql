-- OpsDesk initial schema.
--
-- Conventions
--   * Timestamps are UTC ISO-8601 strings with microseconds and a trailing 'Z'
--     (e.g. 2026-10-18T09:15:02.123456Z). Fixed width => lexicographic order == time order.
--   * Nothing that matters is ever hard-deleted: work items are cancelled, not deleted,
--     and item_events is append-only.
--   * work_items.version is the optimistic-concurrency token. It changes on every
--     user-visible state change of the item (fields, status, assignee, team) and on
--     nothing else (comments and system bookkeeping do not bump it).

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT    NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT    PRIMARY KEY,           -- sha256 of the cookie value; raw token never stored
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE INDEX sessions_expiry ON sessions(expires_at);

CREATE TABLE teams (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,         -- short code shown in the UI, e.g. PAY
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- A user can belong to many teams with a different role in each.
CREATE TABLE memberships (
    team_id INTEGER NOT NULL REFERENCES teams(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role    TEXT    NOT NULL CHECK (role IN ('viewer', 'member', 'lead')),
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX memberships_user ON memberships(user_id);

CREATE TABLE work_items (
    id                    INTEGER PRIMARY KEY,
    team_id               INTEGER NOT NULL REFERENCES teams(id),
    title                 TEXT    NOT NULL,
    description           TEXT    NOT NULL DEFAULT '',
    kind                  TEXT    NOT NULL CHECK (kind IN
                              ('incident', 'customer_issue', 'engineering', 'payment', 'compliance', 'task')),
    priority              INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 4),   -- 1 = most urgent
    status                TEXT    NOT NULL CHECK (status IN
                              ('open', 'in_progress', 'blocked', 'awaiting_approval', 'approved', 'resolved', 'cancelled')),
    active                INTEGER GENERATED ALWAYS AS (status NOT IN ('resolved', 'cancelled')) STORED,
    assignee_id           INTEGER REFERENCES users(id),
    reporter_id           INTEGER NOT NULL REFERENCES users(id),
    requires_approval     INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
    approval_requested_by INTEGER REFERENCES users(id),
    approved_by           INTEGER REFERENCES users(id),
    approved_at           TEXT,
    due_at                TEXT,
    sla_breached_at       TEXT,               -- set once by the SLA sweep; reset when due_at moves
    resolution            TEXT,
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,   -- last versioned change
    last_activity_at      TEXT    NOT NULL,   -- last change OR comment; drives "recently active" and "stale"
    resolved_at           TEXT
);

-- Indexes are shaped around the queries the UI actually issues (see services/queries.py).
CREATE INDEX wi_team_activity   ON work_items(team_id, active, last_activity_at DESC, id DESC);
CREATE INDEX wi_activity        ON work_items(active, last_activity_at DESC, id DESC);
CREATE INDEX wi_activity_all    ON work_items(last_activity_at DESC, id DESC);      -- state=all lists
CREATE INDEX wi_created         ON work_items(active, created_at DESC, id DESC);
CREATE INDEX wi_created_all     ON work_items(created_at DESC, id DESC);
CREATE INDEX wi_priority        ON work_items(active, priority, last_activity_at DESC, id DESC);
CREATE INDEX wi_due             ON work_items(active, COALESCE(due_at, '9999-12-31'), id);  -- matches the 'due' sort
CREATE INDEX wi_assignee        ON work_items(assignee_id, active, priority);
CREATE INDEX wi_reporter        ON work_items(reporter_id, active);
CREATE INDEX wi_team_status     ON work_items(team_id, status);
CREATE INDEX wi_sla_pending     ON work_items(due_at) WHERE active = 1 AND sla_breached_at IS NULL;

-- Full-text search over title + description (external-content FTS5 table kept in sync by triggers).
CREATE VIRTUAL TABLE work_items_fts USING fts5(
    title, description,
    content = 'work_items', content_rowid = 'id',
    tokenize = 'porter unicode61'
);
CREATE TRIGGER work_items_fts_ai AFTER INSERT ON work_items BEGIN
    INSERT INTO work_items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER work_items_fts_ad AFTER DELETE ON work_items BEGIN
    INSERT INTO work_items_fts(work_items_fts, rowid, title, description)
        VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER work_items_fts_au AFTER UPDATE OF title, description ON work_items BEGIN
    INSERT INTO work_items_fts(work_items_fts, rowid, title, description)
        VALUES ('delete', old.id, old.title, old.description);
    INSERT INTO work_items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

-- Append-only history. id is strictly increasing (AUTOINCREMENT never reuses ids) and is
-- also the cursor for the realtime stream and for "load earlier" pagination.
CREATE TABLE item_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES work_items(id),
    team_id      INTEGER NOT NULL REFERENCES teams(id),   -- item's team after the event (feeds, stream filtering)
    actor_id     INTEGER REFERENCES users(id),            -- NULL = system (e.g. SLA sweep)
    kind         TEXT    NOT NULL,
    item_version INTEGER,                                 -- version produced by this event; NULL if not versioned
    data         TEXT    NOT NULL DEFAULT '{}',           -- JSON payload (field diffs, notes, comment body)
    created_at   TEXT    NOT NULL
);
CREATE INDEX item_events_item ON item_events(item_id, id);
CREATE INDEX item_events_team ON item_events(team_id, id);

CREATE TABLE item_watchers (
    item_id    INTEGER NOT NULL REFERENCES work_items(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, user_id)
);
CREATE INDEX item_watchers_user ON item_watchers(user_id);

-- In-app notifications. UNIQUE(event_id, user_id) makes fan-out idempotent:
-- running the same fan-out job twice cannot notify anyone twice.
CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    event_id   INTEGER NOT NULL REFERENCES item_events(id),
    item_id    INTEGER NOT NULL REFERENCES work_items(id),
    reason     TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    read_at    TEXT,
    UNIQUE (event_id, user_id)
);
CREATE INDEX notifications_user ON notifications(user_id, id DESC);
CREATE INDEX notifications_unread ON notifications(user_id) WHERE read_at IS NULL;

-- Durable job queue / transactional outbox. Jobs are inserted in the same transaction as the
-- change that caused them, so a committed change always has its follow-up work recorded and a
-- rolled-back change never leaves an orphan job behind.
CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    kind             TEXT    NOT NULL,
    payload          TEXT    NOT NULL DEFAULT '{}',
    status           TEXT    NOT NULL CHECK (status IN ('pending', 'running', 'done', 'dead')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    run_at           TEXT    NOT NULL,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    last_error       TEXT,
    dedupe_key       TEXT    UNIQUE,          -- e.g. 'sla_sweep:2026-10-18T09:15' => one per minute cluster-wide
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX jobs_ready   ON jobs(run_at) WHERE status = 'pending';
CREATE INDEX jobs_leased  ON jobs(lease_expires_at) WHERE status = 'running';
CREATE INDEX jobs_status  ON jobs(status, updated_at);

-- Idempotency keys: a retried mutation with the same key returns the stored response instead
-- of executing again. Scoped per user; fingerprint catches key reuse with a different request.
CREATE TABLE idempotency_keys (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    key         TEXT    NOT NULL,
    fingerprint TEXT    NOT NULL,
    status_code INTEGER NOT NULL,
    response    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idempotency_created ON idempotency_keys(created_at);
