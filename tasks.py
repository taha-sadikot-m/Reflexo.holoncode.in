"""Cross-platform task runner (Windows has no `make`). Usage: `uv run python tasks.py <task> [args]`.

Tasks:
  up            start infrastructure (Postgres, Redis, Temporal, Temporal UI, Jaeger)
  down          stop infrastructure
  migrate       alembic upgrade head
  seed          load seeds (default mode: onboarding); `seed --full`
  demo-reset    wipe + reseed the demo businesses; `demo-reset --full`
  api           run the HTTP API with reload
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


def run(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    rc = subprocess.call(list(cmd))
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
    elif task == "api":
        run(py, "-m", "uvicorn", "reflexo.api.main:app", "--reload", "--port", "8080")
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
