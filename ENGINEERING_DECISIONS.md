# Engineering decisions

Six decisions that shape OpsDesk, why I made them, and what they cost. Each one names the alternative I
rejected, because most of these could reasonably have gone the other way.

---

## 1. Correctness lives in version-guarded writes, not in the database's locking

**Context.** The brief's hardest requirements are concurrency ones: two people taking the same work, edits
based on stale screens, bursts of updates. I chose SQLite (WAL mode) so a reviewer can run the whole system
with `pip install` and no services, and because at the stated scale (thousands of users, tens of thousands of
active items, short write transactions) a single well-indexed SQLite file is genuinely sufficient for one host.
It also kept the dependency set to three pinned packages that I could verify end to end.

The risk with SQLite is that its global write lock *hides* concurrency bugs: read-check-write code that would
be wrong on PostgreSQL looks correct, because nothing else can write in between.

**Decision.** Every command follows the same five steps inside one transaction — read the row, check policy
against that row, compare the client's `expected_version` where it matters, then
`UPDATE work_items … WHERE id = ? AND version = <version I read>`, then append the history event. If the guarded
`UPDATE` matches zero rows, another transaction got there first and we refuse (`concurrent_modification`)
instead of overwriting. `test_concurrency.py::VersionGuardTests` forces exactly the interleaving that
PostgreSQL's READ COMMITTED allows and checks the guard holds. So SQLite's lock is an optimisation here, not
the correctness argument.

**Trade-offs.**
* SQLite means one writer at a time and one host. A write takes ~1 ms, so throughput is fine for an internal
  tool, but API and workers must share a disk and you cannot scale API nodes across machines.
* Moving to PostgreSQL is mostly mechanical: the guarded `UPDATE`s, unique-constraint idempotency and fenced
  job completion carry over unchanged. What changes: job claiming adds `FOR UPDATE SKIP LOCKED`, the realtime
  tailer needs LISTEN/NOTIFY (see §6), FTS5 becomes `tsvector`, and a driver/pool replaces `sqlite3`.
* **Rejected:** PostgreSQL + `SELECT … FOR UPDATE` everywhere. Stronger for multi-host from day one, but adds
  a service to run for the assessment, and row locks alone don't protect a user whose *screen* is stale —
  that still needs a version check (§2). I would make this move first if the app outgrew one host.

## 2. Three different answers to "someone else changed it", chosen per kind of action

**Context.** "Stale information" is not one problem. Claiming, editing text and approving have different
costs of being wrong.

**Decision.**
* **Claim** is a compare-and-set on "has no owner". No version needed: the precondition *is* the check.
  Exactly one concurrent claimer wins; the others get `409 already_assigned` with the winner, and the UI
  replaces its optimistic "you own this" with the real owner. Claiming your own item again is a no-op.
* **Field edits** (title, description, priority, due date…) send only the changed fields plus the version the
  user started from. If someone changed the item since, the server looks at the history events between the
  two versions: if none of them touched the same fields, it **merges** (applies on top, `merged: true`); if any
  did, it writes nothing and returns `409 edit_conflict` with *your value vs the current value* for each field,
  and the UI lets the user keep theirs, take theirs, or keep editing. Comments don't bump the version, so a
  discussion never makes an edit stale.
* **Decisions** (transitions such as approve/resolve/cancel, and reassignment) are strict: the client sends
  the version it was looking at (the UI always does) and if it differs at all the server refuses with
  `409 stale_version` plus the list of changes since. Approving a refund whose
  amount changed under you should never auto-merge.

**Trade-offs.** Field-level merge needs every versioned change recorded with the fields it touched — which the
history table already does, so it is cheap here. It can still merge two edits that are *semantically* related
(someone shortens the deadline while you raise the priority); I accepted that because both changes are shown
in history and neither is lost. **Rejected:** last-write-wins (silently loses work) and strict versioning for
everything (constant false conflicts on busy items, which trains users to click "overwrite" blindly).

## 3. Idempotency keys are written in the same transaction as the change they protect

**Context.** "A user repeating an action because they are unsure whether the first request succeeded" is the
duplicate-incident / double-comment / double-refund problem.

**Decision.** Every mutating request may carry `Idempotency-Key`. Inside the command's transaction the server
inserts `(user_id, key, fingerprint)`; if the key already exists with the same fingerprint it returns the
stored response (`Idempotent-Replayed: true`) without executing; with a different method/path/body it returns
422. Because the key row commits or rolls back *with* the change, there is no window where the change
happened but the key wasn't recorded, or vice versa. Only successes are stored: if the first attempt failed
(e.g. 409), its key row rolled back too and a retry is evaluated afresh against current state.

On the client, a key represents an *intent*: it stays the same while the payload is the same (double click,
automatic retry after a network error, manual "Retry" on a failed comment) and changes when the user edits
what they are submitting. The **Simulate flaky network** toggle drops the first response of every mutation
after the server processed it, to show the replay working in the UI.

**Trade-offs.** One extra indexed insert per mutation; keys are kept 24 h then cleaned by the maintenance job,
so a retry after a day would execute again. Errors are not replayed (some APIs do replay them); I preferred
re-evaluation because the most common reason to retry a 409 is that the situation changed.

## 4. Side effects go through a transactional outbox with at-least-once workers

**Context.** The brief invites asynchronous work and asks what happens when it fails, runs twice, or is late.

**Decision — what is synchronous:** everything that defines truth: the state change, its history event,
authorization, workflow rules. **Asynchronous:** notifying people (fan-out to watchers, leads, assignee) and
periodic work (SLA breach sweep every minute, hourly cleanup).

* Jobs are rows in `jobs`, inserted in the same transaction as the change (outbox). A rolled-back change
  leaves no job; a committed change always has its job. Tested both ways.
* Workers **lease** jobs (owner + expiry). A crashed worker's job is picked up after the lease expires.
* Completion is **fenced**: `UPDATE jobs SET status='done' WHERE id=? AND lease_owner=? AND attempts=?`, in
  the same transaction as the handler's writes. A slow "zombie" worker whose lease was taken over cannot
  complete, and its writes roll back with it.
* Failures retry with exponential backoff and jitter; after `max_attempts` the job is dead-lettered and
  visible on the admin page with a **Retry** button. A job that keeps killing its worker (lease expires on
  every attempt) also ends up dead instead of looping forever.
* Delivery is at-least-once, so handlers are idempotent: `UNIQUE(event_id, user_id)` on notifications; the SLA
  sweep flags an item with `WHERE sla_breached_at IS NULL`, so overlapping sweeps flag it once.
* Periodic jobs use a `dedupe_key` per time window (`sla_sweep:2026-10-18T09:15`), so any number of workers
  produce one sweep per minute without a leader election.

**Trade-offs.** Notifications are seconds behind the action, and if all workers are down they queue up (no
loss, just delay; the admin page shows queue depth and oldest ready job). Holding SQLite's write lock while a
handler runs means handlers must be short — fine for in-app notifications, and an external call (email) would
instead be a separate job that records delivery by idempotency key. **Rejected:** fire-and-forget threads in
the API process (lost on crash, no retry, no visibility) and an external broker (another service to run, and
it reintroduces the dual-write problem the outbox avoids).

## 5. One policy module decides; the UI only gets hints

**Context.** "Authorization must be enforced by the system itself and should not rely only on hiding
controls."

**Decision.** Per-team roles (viewer < member < lead) plus resource-level rules, all in
`domain/policy.py` as pure functions over (user, current row):
* Visibility: member of the item's team (any role) **or the reporter**. This lets Support raise a request in
  Payments' queue and follow it, without seeing Payments' other work. Invisible items are 404 on detail,
  history and every command, and are simply absent from lists, full-text and `#id` search and the live
  stream — so existence doesn't leak.
* Ownership: members claim unowned work; only a lead (or the current owner handing off) assigns others; an
  assignee must be a member of the item's team.
* Separation of duties: the approver can't be the owner or the requester; only leads can waive the approval
  requirement, and not while an approval is in flight.

Every command calls these checks inside its transaction, on the row it is about to write. The API also
returns `allowed_actions` per item, computed by calling the *same* checks, so the UI and the server cannot
disagree about what is allowed — but the server never trusts the UI. Sessions are server-side (sha256 of an
HttpOnly cookie) so roles are re-read on every request and logout is immediate; mutations also require a
custom header as CSRF defence in depth on top of SameSite=Lax.

**Trade-offs.** Roles are coarse (three levels per team). A finer model (per-action permissions, per-kind
approvers) would slot into the same module. Membership is read per request — one indexed query — which is
cheaper than invalidating cached permissions correctly.

## 6. Live updates: SSE fed by tailing the event log, and events are hints, not data

**Context.** "One user viewing information that another user has just changed."

**Decision.** Each API process runs one background task that polls `item_events WHERE id > cursor` once a
second (one cheap indexed query per process, independent of the number of connected browsers) and pushes
`{item_id, kind, item_version, actor}` over Server-Sent Events to subscribers allowed to see that item. The
browser never applies pushed data directly: it invalidates the affected cache entries and refetches through the
normal API, so authorization and serialization live in one place and a dropped event costs freshness, not
correctness. SSE `id:` is the event id, so on reconnect the browser's `Last-Event-ID` lets the server replay
what was missed; past 500 events, or if a subscriber's buffer overflows, it sends `resync` and the client
refetches what is on screen. The client cache also refuses to replace data with an older `version`, so a slow
response can't undo a newer one.

UX choices on top: the item page refreshes in place and says who changed what, but **never touches an open
edit draft** (it shows "changed since you started" and the save path from §2 decides). The list page does not
reshuffle under the user's cursor; it shows "N updates · Refresh".

**Trade-offs.** Up to ~1 s latency. Tailing by id is gap-free on SQLite because commits are serialized in id
order; on PostgreSQL an id can commit after a larger one, so there I'd wake on LISTEN/NOTIFY and re-read a short
trailing window (or tail by commit order). A subscriber's team list is captured at connect time.
**Rejected:** WebSockets (bidirectional isn't needed; SSE reconnects and resumes for free over plain HTTP) and
client polling of every list (load grows with users × open tabs).

---

## Smaller decisions worth knowing

* **Keyset pagination, capped counts.** Lists seek on `(sort columns…, id)` with mixed ASC/DESC support, so
  page N costs the same as page 1 and rows don't duplicate when new items arrive. Dashboard counts stop at 1,000.
  Indexes match the four sort orders; measured 0.4–2 ms per page on 40k items.
* **Search is a filter, not a ranking.** FTS5 prefix match on every word, user input quoted token-by-token so
  FTS syntax can't be injected. Results keep the chosen sort so pagination stays stable.
* **History is the audit log.** Append-only `item_events`; each versioned event records the version it produced
  (`test_workflow.py` checks versions 1..N map to exactly one event each). Nothing important is deleted — items
  are cancelled, not removed.
* **Status changes are not optimistic in the UI; claims and comments are.** A claim is cheap to roll back and
  benefits from instant feedback; an approval is a decision the user should see the server confirm.
* **Starlette + Pydantic rather than a larger framework.** Routes are deliberately thin (`query()` = read
  snapshot, `command()` = one write transaction), so business behaviour is tested at the service layer
  without HTTP, and the HTTP tests exercise the real server.

## What I deliberately did not build

* Team/membership administration UI (seeded instead) — authorization is the interesting part, not CRUD.
* Email/Slack delivery — the queue is designed for it; in-app notifications exercise the same guarantees.
* SSO, password reset, rate limiting — standard, but not where this exercise's risk is.
* Editable/deletable comments — conflicts with append-only history; corrections are new comments.
* Drag-and-drop boards, rich text, attachments — decorative relative to the coordination problem.
