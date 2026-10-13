# Deploying the POC on one server

One Ubuntu 24.04 host (c6i.2xlarge, 100 GB) runs everything in Docker; only Caddy (80/443) is reachable from
outside. Voice uses LiveKit Cloud, so no media ports are opened on the server.

## First time

1. Security group: inbound 22 (your IP), 80 and 443 (anywhere).
2. On the server: `sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER`
   (log out and in once).
3. Copy the repo to `/opt/reflexo` (`git archive HEAD` + the `.env` built for this host — never commit it).
   The host name is a real domain, or `<ip-with-dashes>.sslip.io` until one exists; Caddy gets the certificate.
4. `cd /opt/reflexo && docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d --build`
5. Seed the demo businesses once: `docker compose -f deploy/docker-compose.prod.yml --env-file .env exec api python scripts/seed.py reset --full`
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
| postgres, redis, temporal | state, queues, workflows (127.0.0.1 only) |
