# REFLEXO (proof of concept)

REFLEXO is an AI employee for Indian product sellers: online shops, home bakers, Instagram sellers. It does four things:

- Answers customers on WhatsApp (chat and calls), Telegram, Messenger and Instagram.
- Takes orders and payments.
- Learns from the owner.
- Runs automations that the owner describes in plain language.


## Architecture (POC_01)

The code is one Python codebase (a modular monolith) that runs as several processes:

| Process | Role |
|---|---|
| `api` | Webhooks, admin API, health check |
| `worker` | Inbound, turns, outbound and jobs |
| `temporal-worker` | Durable workflows |
| `voice-bridge` | WhatsApp WebRTC ↔ LiveKit |
| `voice-agent` | LiveKit Agents |

How the parts fit together:

- **Postgres** is the source of truth and the queue.
- **Redis** is a disposable real-time layer.
- **Temporal** runs everything that waits.
- **LangGraph** drives the agent turns.
- **Postgres row-level security (RLS)** isolates each business's data.

## Quick start (Windows or Linux)

```bash
cp .env.example .env                    # fill in values
uv sync                                 # Python 3.12 + deps
uv run python tasks.py up               # Postgres+pgvector, Redis, Temporal (:7243, UI :8098), Jaeger (:16686)
uv run python tasks.py migrate          # alembic upgrade head (schema + roles grants + RLS)
uv run python tasks.py demo-reset       # seed demo businesses (add --full for the full catalogue)
uv run python tasks.py api              # http://localhost:8080/healthz
```

On Linux, macOS, AWS or CI, `make <task>` wraps the same commands (`make demo-reset FULL=1`).

## Quality gates

```bash
uv run python tasks.py lint        # ruff, format check, module boundaries, forbidden patterns
uv run python tasks.py typecheck   # mypy (strict on common/pipeline/gateway/voice)
uv run python tasks.py test        # unit + integration (integration needs Docker)
```

- **Module boundaries** (`scripts/check_boundaries.py`): a module may import another module only through `reflexo.<module>.api`, and only where `POC_01` §4.1 allows that dependency.
- **Forbidden patterns** (`scripts/check_guards.py`): `eval`/`exec`, LangGraph `interrupt()` and stores, and any names from the old architecture.

## Harvested code

Proven provider code from the old repo is harvested in four steps: copy → adapt → harden → test, against the quality bar in `POC_01` §12. Every harvested file is recorded in `docs/harvest_log.md`.
