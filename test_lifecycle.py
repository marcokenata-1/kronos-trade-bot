"""assert-based self-check: with --exit-when-closed the server stops when the page says goodbye but survives a
reload; without the flag it never stops on its own. Runs a throwaway copy with the grace period shortened."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def start(workdir, *flags):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-u", "bot.py", "web", "--port", str(port), *flags], cwd=workdir,
                            stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    while "dashboard at" not in proc.stdout.readline():
        assert proc.poll() is None, "server died on startup"
    return proc, f"http://127.0.0.1:{port}"


def bye(url):
    return requests.post(url + "/api/bye", headers={"Origin": url})


def stopped_within(proc, seconds):
    try:
        return proc.wait(timeout=seconds) == 0
    except subprocess.TimeoutExpired:
        return False


def demo():
    with tempfile.TemporaryDirectory() as d:
        src = (Path(__file__).parent / "bot.py").read_text()
        short = src.replace("BYE_GRACE = 8 ", "BYE_GRACE = 1 ")
        assert short != src, "constants moved: update this test"
        (Path(d) / "bot.py").write_text(short)
        shutil.copy(Path(__file__).parent / "dashboard.html", d)
        procs = []
        try:
            # closed: goodbye, nobody comes back -> exits cleanly
            proc, url = start(d, "--exit-when-closed"); procs.append(proc)
            assert requests.get(url + "/").status_code == 200
            assert bye(url).status_code == 200
            assert stopped_within(proc, 6), "must exit after the page says goodbye"

            # reload: goodbye then a request straight away -> stays up
            proc, url = start(d, "--exit-when-closed"); procs.append(proc)
            bye(url); time.sleep(0.3)
            assert requests.get(url + "/").status_code == 200
            time.sleep(2.5)
            assert proc.poll() is None and requests.get(url + "/").status_code == 200, "a reload must not stop the server"

            requests.post(url + "/api/bye", headers={"Origin": url})  # let the reload-survivor go so it doesn't linger
            assert stopped_within(proc, 6)

            # plain `web` (no flag) is a normal always-on server
            proc, url = start(d); procs.append(proc)
            bye(url); time.sleep(3)
            assert proc.poll() is None and requests.get(url + "/").status_code == 200

            # a foreign site can't shut it down
            proc2, url2 = start(d, "--exit-when-closed"); procs.append(proc2)
            assert requests.post(url2 + "/api/bye", headers={"Origin": "http://evil.example"}).status_code == 403
            time.sleep(2.5)
            assert proc2.poll() is None
        finally:
            for p in procs:
                p.kill()
    print("ok")


if __name__ == "__main__":
    demo()
