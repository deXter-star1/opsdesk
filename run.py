"""One-command local run (Windows, macOS, Linux).

    python run.py                 # migrate, seed if empty (40k items), start API + worker
    python run.py --reset         # wipe and reseed first
    python run.py --items 3000    # smaller dataset
    python run.py --failure-rate 0.3   # worker randomly fails 30% of jobs (to watch retries)

Then open http://localhost:8000 and pick a demo account (password: demo1234).
"""
from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--items", type=int, default=40000)
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--failure-rate", type=float, default=0.0)
    args = ap.parse_args()

    env = dict(os.environ)
    env.setdefault("OPSDESK_DB", str(BACKEND / "data" / "opsdesk.db"))
    env["OPSDESK_JOB_FAILURE_RATE"] = str(args.failure_rate)
    db_path = Path(env["OPSDESK_DB"])

    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("frontend/dist is missing. Build it with: cd frontend && npm install && npm run build")
        return 1

    def has_data() -> bool:
        if not db_path.exists():
            return False
        try:
            with sqlite3.connect(db_path) as c:
                return c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        except sqlite3.Error:
            return False

    py = sys.executable
    if args.reset or not has_data():
        seed_cmd = [py, "-m", "app.seed", "--items", str(args.items), "--users", str(args.users)]
        if args.reset:
            seed_cmd.append("--reset")
        subprocess.run(seed_cmd, cwd=BACKEND, env=env, check=True)
    else:
        subprocess.run([py, "-m", "app.migrate"], cwd=BACKEND, env=env, check=True)

    procs = [
        subprocess.Popen([py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
                         cwd=BACKEND, env=env),
        subprocess.Popen([py, "-m", "app.worker"], cwd=BACKEND, env=env),
    ]
    print(f"\n  OpsDesk running at http://localhost:{args.port}   (Ctrl+C to stop)\n")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT) if os.name != "nt" else p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
