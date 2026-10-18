# Thin wrapper over tasks.py for Linux/macOS/AWS/CI. On Windows use: uv run python tasks.py <task>
T = uv run python tasks.py

.PHONY: up down migrate seed demo-reset eval benchmark policy-check api dev test lint typecheck check
up: ; $(T) up
down: ; $(T) down
migrate: ; $(T) migrate
seed: ; $(T) seed $(if $(FULL),--full,)
demo-reset: ; $(T) demo-reset $(if $(FULL),--full,)
eval: ; $(T) eval $(SUITE)
benchmark: ; $(T) benchmark $(or $(ARGS),run)
policy-check: ; $(T) policy-check
api: ; $(T) api
dev: ; $(T) dev
test: ; $(T) test
lint: ; $(T) lint
typecheck: ; $(T) typecheck
check: ; $(T) check
