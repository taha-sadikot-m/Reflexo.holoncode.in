"""Cross-platform task runner (Windows has no `make`). Usage: `uv run python tasks.py <task> [args]`.

Tasks:
  up            start infrastructure (Postgres, Redis, Temporal, Temporal UI, Jaeger)
  down          stop infrastructure
  migrate       alembic upgrade head
  seed          load seeds (default mode: onboarding); `seed --full`
  demo-reset    wipe + reseed the demo businesses; `demo-reset --full`
  eval          eval suites with real Gemini (POC_08 §4); `eval tool_selection grounding`
  api           run the HTTP API with reload
  worker        run the pipeline worker (all pools)
  voice-bridge  run the voice-bridge (WhatsApp WebRTC <-> LiveKit)
  voice-agent   run the LiveKit voice agent worker (`voice-agent dev` for verbose local runs)
  admin-dev     admin panel dev server (http://localhost:5173/admin/)
  admin-build   build the admin panel into apps/admin/dist (served at http://localhost:8080/admin/)
  dev           up + migrate + api
  test          pytest (unit + integration; integration needs Docker)
  lint          ruff check + ruff format --check + module boundaries + guards
  typecheck     mypy
  check         lint + typecheck + test
"""

from __future__ import annotations

import subprocess
import sys
import time


def run(*cmd: str, cwd: str | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    # npm is a .cmd shim on Windows, which subprocess only finds through the shell
    rc = subprocess.call(list(cmd), cwd=cwd, shell=sys.platform == "win32" and cmd[0] == "npm")
    if rc != 0:
        sys.exit(rc)


def wait_for_postgres(timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc = subprocess.call(
            ["docker", "compose", "exec", "-T", "postgres", "pg_isready", "-U", "reflexo_owner", "-d", "reflexo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc == 0:
            return
        time.sleep(1)
    sys.exit("postgres did not become ready")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    task, args = argv[0], argv[1:]
    py = sys.executable
    if task == "up":
        run("docker", "compose", "up", "-d")
        wait_for_postgres()
    elif task == "down":
        run("docker", "compose", "down")
    elif task == "migrate":
        run(py, "-m", "alembic", "upgrade", "head")
    elif task == "seed":
        run(py, "scripts/seed.py", "load", *args)
    elif task == "demo-reset":
        run(py, "scripts/seed.py", "reset", *args)
    elif task == "eval":
        run(py, "scripts/eval.py", *args)
    elif task == "api":
        run(py, "-m", "uvicorn", "reflexo.api.main:app", "--reload", "--port", "8080")
    elif task == "worker":
        run(py, "-m", "reflexo.pipeline.worker", *args)
    elif task == "voice-bridge":  # owns live WhatsApp WebRTC calls
        run(py, "-m", "reflexo.voice.bridge_worker")
    elif task == "voice-agent":  # LiveKit Agents worker `reflexo-voice` (dev | start)
        run(py, "-m", "reflexo.voice.agent_worker", *(args or ["start"]))
    elif task == "admin-dev":  # Vite dev server on :5173, proxies /admin/api to :8080
        run("npm", "run", "dev", cwd="apps/admin")
    elif task == "admin-build":  # builds apps/admin/dist (served by the API at /admin locally, Caddy in the demo)
        run("npm", "ci", "--no-audit", "--no-fund", cwd="apps/admin")
        run("npm", "run", "build", cwd="apps/admin")
    elif task == "dev":
        main(["up"])
        main(["migrate"])
        main(["api"])
    elif task == "test":
        run(py, "-m", "pytest", *args)
    elif task == "lint":
        run(py, "-m", "ruff", "check", ".")
        run(py, "-m", "ruff", "format", "--check", ".")
        run(py, "scripts/check_boundaries.py")
        run(py, "scripts/check_guards.py")
    elif task == "typecheck":
        run(py, "-m", "mypy")
    elif task == "check":
        main(["lint"])
        main(["typecheck"])
        main(["test"])
    else:
        sys.exit(f"unknown task: {task}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
