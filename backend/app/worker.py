"""Background worker: runs queued jobs and schedules periodic ones.

    python -m app.worker

Run as many workers as you like. Periodic jobs are enqueued with a dedupe_key per time
window (e.g. 'sla_sweep:2026-10-18T09:15'), so N workers still produce one sweep per minute.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid

from . import config, db
from .jobs import queue
from .jobs.handlers import HANDLERS, InjectedFailure
from .timeutil import utcnow

log = logging.getLogger("opsdesk.worker")

PERIODIC = [
    # (job kind, window format) -> one job per window across all workers
    ("sla_sweep", "%Y-%m-%dT%H:%M"),       # every minute
    ("maintenance", "%Y-%m-%dT%H"),        # every hour
]


def schedule_periodic() -> None:
    now = utcnow()
    with db.write_tx() as conn:
        for kind, window in PERIODIC:
            queue.enqueue(conn, kind, {}, dedupe_key=f"{kind}:{now.strftime(window)}", max_attempts=3)


def process(job: dict, worker_id: str) -> str:
    """Run one leased job. Returns 'done', 'pending' (will retry), 'dead' or 'lease_lost'."""
    handler = HANDLERS.get(job["kind"])
    try:
        with db.write_tx() as conn:
            if handler is None:
                raise RuntimeError(f"no handler for job kind {job['kind']!r}")
            result = handler(conn, job["payload"])
            queue.complete(conn, job, worker_id)  # fenced: rolls back the handler's writes if lease lost
        log.debug("job %s %s done %s", job["id"], job["kind"], result)
        return "done"
    except queue.LeaseLost as exc:
        log.warning("%s", exc)
        return "lease_lost"
    except Exception as exc:  # handler failed: its writes were rolled back, record the attempt
        err = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, InjectedFailure):
            err += "\n" + traceback.format_exc(limit=3)
        try:
            with db.write_tx() as conn:
                status = queue.fail(conn, job, worker_id, err)
        except queue.LeaseLost as lost:
            log.warning("%s", lost)
            return "lease_lost"
        log.info("job %s %s failed (attempt %s/%s) -> %s: %s", job["id"], job["kind"], job["attempts"],
                 job["max_attempts"], status, str(exc)[:200])
        return status


def run_once(worker_id: str, batch: int = 20) -> int:
    """Claim and process one batch. Returns number of jobs processed. Used by tests and the loop."""
    with db.write_tx() as conn:
        jobs = queue.claim(conn, worker_id, limit=batch, lease_seconds=config.settings.job_lease_seconds)
    for job in jobs:
        process(job, worker_id)
    return len(jobs)


def run_forever(stop: threading.Event) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    log.info("worker %s started (db=%s, failure_rate=%s)", worker_id, config.settings.database_path,
             config.settings.job_failure_rate)
    last_schedule = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() - last_schedule >= 10:
                schedule_periodic()
                last_schedule = time.monotonic()
            if run_once(worker_id) == 0:
                stop.wait(config.settings.worker_poll_seconds)
        except Exception:  # never let one bad iteration kill the worker (e.g. transient lock timeout)
            log.exception("worker loop error")
            stop.wait(2)
    log.info("worker %s stopped", worker_id)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run_forever(stop)


if __name__ == "__main__":
    main()
