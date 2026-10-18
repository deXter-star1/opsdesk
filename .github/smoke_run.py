"""CI smoke test: start the app with run.py exactly as a reviewer would, then check it end to end.

Checks: the UI is served, a demo user can log in and list items, the worker is processing jobs,
and run.py shuts its child processes down on Ctrl+C (POSIX). Exits non-zero on any failure.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Requested-With": "smoke"})
    with opener.open(req, timeout=10) as resp:
        raw = resp.read()
        return resp.status, raw


def wait_for_health(deadline: float) -> None:
    while time.time() < deadline:
        try:
            if call("GET", "/api/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("FAIL: app did not become healthy")


def main() -> int:
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, OPSDESK_DB=os.path.join(tmp, "smoke.db"))
    log_path = os.path.join(tmp, "run.log")
    log = open(log_path, "w")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    proc = subprocess.Popen([sys.executable, "run.py", "--reset", "--items", "500", "--users", "50",
                             "--port", str(PORT)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            **kwargs)
    ok = False
    try:
        wait_for_health(time.time() + 180)

        status, html = call("GET", "/")
        assert status == 200 and b'<div id="root"' in html, "UI not served"
        print("PASS  UI served")

        status, _ = call("POST", "/api/auth/login", {"email": "admin@opsdesk.test", "password": "demo1234"})
        assert status == 200, f"login returned {status}"
        print("PASS  demo login")

        items = json.loads(call("GET", "/api/items?limit=5")[1])["items"]
        assert items, "no items listed"
        print(f"PASS  listed {len(items)} items")

        deadline = time.time() + 60
        while True:
            done = json.loads(call("GET", "/api/admin/jobs")[1])["stats"]["by_status"]["done"]
            if done > 0:
                print(f"PASS  worker completed {done} job(s)")
                break
            if time.time() > deadline:
                raise AssertionError("worker completed no jobs in 60 s")
            time.sleep(1)
        ok = True
    except Exception as e:  # noqa: BLE001 - report any failure, then show the app's log
        print(f"FAIL  {e}")
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.send_signal(signal.SIGINT)  # the same path as a user pressing Ctrl+C
        try:
            code = proc.wait(timeout=20)
            if os.name != "nt" and ok and code != 0:
                print(f"FAIL  run.py exited with {code} on Ctrl+C")
                ok = False
            elif os.name != "nt" and ok:
                print("PASS  clean shutdown on Ctrl+C")
        except subprocess.TimeoutExpired:
            proc.kill()
            print("FAIL  run.py did not stop within 20 s")
            ok = False
        log.close()
        if not ok:
            print("---- run.py output ----")
            print(Path(log_path).read_text(errors="replace")[-6000:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
