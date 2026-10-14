# Deploying the POC on one server

One Ubuntu 24.04 host (m6i.large: 2 vCPU, 8 GB; 30 GB disk) runs the app in Docker; only Caddy (80/443) is
reachable from outside. The database is managed Postgres (Neon) and Temporal runs on its own server; both are
addressed from `.env`. Voice uses LiveKit Cloud, so no media ports are opened on the server. Put the server in the
database's region: the app makes many small queries per reply.

## First time

1. Security group: inbound 22 (your IP), 80 and 443 (anywhere).
2. On the server: `sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER`
   (log out and in once).
3. Copy the repo to `/opt/reflexo` (`git archive HEAD` + the `.env` built for this host — never commit it).
   The host name is a real domain, or `<ip-with-dashes>.sslip.io` until one exists; Caddy gets the certificate.
4. `cd /opt/reflexo && docker compose -f deploy/docker-compose.prod.yml -f deploy/docker-compose.localdb.yml --env-file .env up -d --build`
   (`docker-compose.localdb.yml` runs Postgres on this server. Leave it out only when the managed database is in
   the same region: measured, a database ~180 ms away made replies take ~65 s instead of ~6 s.)
5. Seed the demo businesses once (if the database is new): `docker compose -f deploy/docker-compose.prod.yml --env-file .env exec api python scripts/seed.py reset --full`
6. Point the provider webhooks at `https://<host>`: Meta WhatsApp (`/webhooks/meta_whatsapp`), Razorpay
   (`/integrations/webhooks/razorpay`), Telegram (`/webhooks/telegram/<connection id>`).

## Updating

Copy the new code, then the same `up -d --build`. Migrations run when the API starts.

## What runs

| Service | Role |
|---|---|
| caddy | HTTPS, reverse proxy to the API |
| api | public site, owner app, ops console, webhooks, admin API |
| worker | pipeline (turns, sends, media, embeddings) |
| temporal-worker | imports, automations, approvals |
| voice-agent | LiveKit Agents worker (WhatsApp and browser calls) |
| voice-bridge | WhatsApp WebRTC calls ↔ LiveKit (host network) |
| redis | wake-ups, rate limits, live call signals (127.0.0.1 only) |
