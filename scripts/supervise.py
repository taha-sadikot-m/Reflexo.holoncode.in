"""Restart a process when it dies — locally what Docker's `restart: unless-stopped` does on AWS.

    uv run python scripts/supervise.py -m reflexo.pipeline.worker

Used by the "kill worker mid-reply" chaos button: the worker exits hard, this restarts it, and the half-done
turn resumes from its checkpoint on the next attempt. Stops on Ctrl+C or if the child crashes 5 times in a minute.
"""

from __future__ import annotations

import subprocess
import sys
import time

BACKOFF_S = 1.0
MAX_CRASHES_PER_MIN = 5


def main() -> None:
    args = [sys.executable, *sys.argv[1:]]
    if len(args) < 2:
        sys.exit("usage: supervise.py -m <module> [args...]")
    crashes: list[float] = []
    restarts = 0
    while True:
        started = time.monotonic()
        child = subprocess.Popen(args)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            child.wait(timeout=10)
            return
        now = time.monotonic()
        crashes = [t for t in crashes if now - t < 60] + [now]
        if len(crashes) > MAX_CRASHES_PER_MIN:
            sys.exit(f"supervise: child crashed {len(crashes)} times in a minute; giving up (last code {code})")
        restarts += 1
        print(f"supervise: child exited with {code} after {now - started:.1f}s; restart #{restarts}", flush=True)
        time.sleep(BACKOFF_S)


if __name__ == "__main__":
    main()
