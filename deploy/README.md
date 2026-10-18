# Deploy files

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | The app on one server: api, worker, temporal-worker, voice-agent, voice-bridge, Redis, Caddy. Database and Temporal addresses come from `.env` |
| `docker-compose.localdb.yml` | Overlay: Postgres on the same server (use it unless a managed database is in the same region) |
| `Caddyfile` | HTTPS for `REFLEXO_DOMAIN`, redirects from `REFLEXO_REDIRECT_FROM`, static legal pages on `REFLEXO_LEGAL_DOMAIN` (from `deploy/legal-site`) |
| `postgres/init/` | Database roles created on first start |

The topology, the latency measurements behind the choices, sizing, security and the runbook are in
[docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).
