"""assert-based self-check: the dashboard restarts itself when bot.py changes (run against a throwaway copy)."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def wait_for(proc, text, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if text in line:
            return
    raise AssertionError(f"never saw {text!r}")


def demo():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with tempfile.TemporaryDirectory() as d:
        for f in ("bot.py", "dashboard.html"):
            shutil.copy(Path(__file__).parent / f, d)
        proc = subprocess.Popen([sys.executable, "-u", "bot.py", "web", "--port", str(port)], cwd=d,
                                stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        try:
            wait_for(proc, "dashboard at")
            assert requests.get(f"http://127.0.0.1:{port}/").status_code == 200
            future = time.time() + 5
            os.utime(Path(d) / "bot.py", (future, future))  # what saving an edit does
            wait_for(proc, "changed, restarting dashboard")
            wait_for(proc, "dashboard at")  # same process (exec), new code
            assert requests.get(f"http://127.0.0.1:{port}/").status_code == 200, "must serve again after the restart"
        finally:
            proc.kill()
    print("ok")


if __name__ == "__main__":
    demo()
