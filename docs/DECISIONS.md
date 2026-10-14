# Decisions

The decisions that shaped Reflexo, in the order they matter. Each says what problem it answers, what was chosen,
what else was considered, and what it costs. Where a measurement settled a decision, the numbers are here.

---

## Product and agent

### D1. The agent is a learning employee, not a set of coded flows

**Problem.** Early live testing showed that every missing behaviour (cancel a paid order? reconfirm cash on
delivery? refund?) needed a new hand-written flow, and the agent once contradicted the owner's own automation
because it could not see it. Every business works differently; a coded flow per behaviour cannot scale to "any
business".

**Decision.** No business flow is coded in tools or prompts.
- **Capabilities** are generic typed actions with only hard invariants and a risk class (routine, commitment,
  money).
- **The playbook** is how this business works, taught by the owner: facts (Q&A), rules ("when X, may / may not
  do Y", compiled to deterministic conditions) and procedures (automations). Every entry has a scope, a
  provenance and an undo.
- **One decision function**, `decide()`: invariants, then the owner's rules, then the default for the risk class.
  If nothing covers a case, the agent proposes the exact action to the owner; the owner's decision executes as
  proposed; the agent offers to remember it ("always / only for this customer / just this once").

**Alternatives.** Hand-written flows per behaviour (does not scale, contradicts the owner); letting the model
decide (unsafe: it would authorise itself).

**Consequences.** Every autonomous action is traceable to an owner decision or rule. The cost is a richer data
model (capabilities, rules, proposals) and a read-back step for every learned rule.

### D2. Fix the cause, never the example

**Problem.** It is tempting to fix an AI failure by adding the failing phrase to a prompt or a keyword list. Each
such patch fixes one sentence and breaks another.

**Decision.** Every defect gets a root-cause fix in code or data, plus a regression test, and no phrase lists in
prompts. Examples from this build, each found by driving the product end to end:
- A typed menu was ignored because the import pipeline only accepted files. Fix: a typed list is a source like a
  file (`import_from_text`), with prices verified against the text. Plus a guard test so a tool tagged for a
  desk can never be missing from the list that desk binds.
- Correct answers fell back to "I'll check with the owner" because the model omitted one schema field. Fix: the
  schema infers the field when the content makes it unambiguous, instead of rejecting.
- A message sent as a caller hung up was silently lost. Fix: the call ends the moment the caller leaves, routing
  locks the call row, and anything the call never answered goes back to the chat AI.

More in [QUALITY.md](QUALITY.md).

### D3. A deterministic gate on every customer-visible message

**Problem.** Language models invent prices, discounts and links with full confidence.

**Decision.** Every outgoing customer message passes deterministic code that checks references, amounts,
discounts, links, contacts and grounding against the shop's records and this turn's tool results. A failure gets
one repair round with exact guidance, then falls back to asking the owner.

**Alternatives.** A second model as a judge (slower, can be wrong the same way); prompting harder (no guarantee).

**Consequences.** Measured: 100% of invented prices and discounts blocked on an adversarial suite, with false
blocks tracked by their own evals. The gate needs the turn's facts, so tools return structured data.

### D4. Voice is a channel, not a second brain

**Decision.** The LiveKit voice agent uses the same tools, rules and grounding as chat. WhatsApp calls reach
LiveKit through a WebRTC bridge; browser calls join the room directly. A photo sent in the chat during a call
reaches the voice agent. At the end of a call, everything the agent promised is reconciled against what happened.

**Alternatives.** A separate voice agent with its own prompts and tools (two sources of truth that drift apart).

**Consequences.** One place to change behaviour. The voice prompt is tuned for speech (short sentences, prices as
words), but its facts come from the same tools.

### D5. The demo is the real product on simulated phones

**Problem.** A reviewer must be able to see every feature without a WhatsApp number, and two visitors must never
see each other's orders, questions or learned rules.

**Decision.** Each visitor gets a private sandbox: a copy of the demo bakery whose channels are simulated
(WhatsApp-shaped, signed payloads into the real ingress; only the last hop to the provider is replaced). The
visitor is signed in as that shop's owner, so the owner dashboard shows the same shop. Sandboxes expire after 24
hours; creation is limited per client and per day.

**Alternatives.** One shared demo shop (visitors collide); a scripted or recorded demo (not the real system).

**Consequences.** The demo exercises the real pipeline, gate, learning loop and voice. Nothing in a sandbox can
reach a real customer, owner or payment.

### D6. One language, enforced by policy

**Decision.** The platform writes and speaks English to everyone (`language_policy = en`); it still understands
Hindi and Hinglish. One setting drives the reply language, the prompts' language rule, the voice greeting and every
fixed system message. `auto` restores mirroring the customer's language and script; both are tested.

**Detail that mattered.** The language detector weighs Hindi and English function words and, when the evidence is
unclear, keeps the established language. A short mixed message must not flip a conversation.

### D7. House style is enforced, not hoped for

**Decision.** No long em-dash anywhere a person reads. The source text was rewritten; the model is told; and as a
guarantee, every outgoing message and the voice stream are normalised at the edge, and the web app normalises
everything the API returns. Guard tests keep em-dashes out of the web app, prompts, seeds and user-facing strings.

**Why this approach.** A style rule that depends on the model alone will eventually fail; normalising at the
boundary makes it a property of the system.

---

## Architecture

### D8a. Rebuild on a new architecture, harvest what was proven

**Context.** An earlier Reflexo codebase ([reflexo_code](https://github.com/taha-sadikot-m/reflexo_code)) worked in
places (Meta webhook parsing, the WhatsApp call bridge, payload limits per channel) but grew around shortcuts:
separate paths and tables for Meta and non-Meta messages, duplicated identity code, platform-specific send tools.

**Decision.** Start a new codebase on the architecture described here, and bring over only code that had been
proven in production, through four steps (copy, adapt, harden, test) and an eight-point quality bar: new module
boundaries and names, fully typed, idempotent handlers, masked logs and tracing, registry error codes, ported plus
new tests, no restart-critical state in memory, no secrets. Every harvested file is recorded in
[harvest_log.md](harvest_log.md).

**Alternatives.** Refactoring the old code in place (the shortcuts were structural); rewriting everything (throws
away hard-won provider details such as WhatsApp's WebRTC rules).

**Consequences.** The proven parts arrived with their tests and with the edge cases they had already survived;
everything around them follows one design.

### D8. A modular monolith

**Decision.** One codebase, many processes, strict module boundaries (`<module>.api` only, checked in CI), ports
to break cycles.

**Alternatives.** Microservices (network calls, distributed transactions and deployment overhead with no benefit
at this team size).

**Consequences.** Refactoring across modules is cheap; any module can later become a service along its existing
API.

### D9. Postgres is both the source of truth and the queue

**Decision.** Webhooks go into `inbox_events` first. Workers claim rows with `FOR UPDATE SKIP LOCKED` and are woken
by `LISTEN/NOTIFY`. Redis holds only disposable data (wake-ups, rate limits, live signals, caches); every Redis use
has a Postgres fallback, and the system keeps working with Redis down (a chaos test proves it).

**Alternatives.** Celery or a broker (Kafka, SQS): a second store to keep consistent with the database, and lost
or duplicated work on the boundary.

**Consequences.** Enqueueing is part of the same transaction as the data change, so nothing is half-done. The
throughput ceiling is far above what a POC needs.

### D10. Temporal for everything that waits

**Decision.** Imports, automation runs (waits, timers, approvals, counters), onboarding previews and automation
building are Temporal workflows. Waits run on a demo time scale (one day = 60 s) for live demos.

**Alternatives.** Cron jobs and database polling (fragile across restarts, hard to reason about).

**Consequences.** A reminder scheduled for tomorrow survives restarts and deploys. Temporal is one more piece of
infrastructure; the deployment reuses an existing Temporal server.

### D11. Row-level security for multi-tenancy

**Decision.** Every tenant table has a `business_id` and a Postgres RLS policy; every transaction sets the tenant.
The app role has forced RLS; a separate service role handles steps before the tenant is known.

**Alternatives.** Filtering by `business_id` in application code (one forgotten `WHERE` leaks a shop's data).

**Consequences.** Isolation holds even if application code is wrong. Tests assert every tenant table has a policy
and that the app role without a tenant sees nothing.

### D12. Owner identity requires a verified binding

**Decision.** A sender is treated as the owner only through a verified binding for that channel and id; owner web
sessions are locked to their business server-side, use their own cookie, and cannot reach engineering endpoints.

**Why.** Owner commands change prices and policies; a phone-number string match is not authentication.

---

## Deployment

### D13. One VM with Docker Compose, not a managed platform

**Decision.** One EC2 instance runs the app with Docker Compose; Caddy provides HTTPS with automatic certificates.
The production image builds the web app in a separate stage; migrations run when the API starts.

**Alternatives.** ECS/Fargate, RDS, ElastiCache, Terraform: right for production, too much setup and cost for a
two-week proof of concept.

**Consequences.** Deploy is `git archive` to the server plus one compose command. The compose files are written
so the database can move to a managed service by changing environment variables only.

### D14. The database lives next to the app (measured)

**Context.** A managed Postgres (Neon) was available in Singapore; the server runs in Stockholm.

**Measurement** (round trips from the server, and real reply times on the live site):

| | Round trip | Chat reply | Voice call ready |
|---|---|---|---|
| Managed Postgres, Singapore | ~181 ms | 62 to 73 s | 20+ s |
| Postgres on the same server | under 1 ms | 4 to 9 s | 4 to 6 s |

A single reply makes a few hundred small queries (tenant session, routing, tools, X-ray events, the outbound
queue), so round-trip time multiplies.

**Decision.** Postgres runs on the server (`deploy/docker-compose.localdb.yml`). The managed database was set up,
migrated and verified (roles, RLS, LISTEN, the conversation-state store), so moving to it later is a configuration
change, made only when it is in the same region as the app.

**What it surfaced.** Drivers used directly (asyncpg `LISTEN`, psycopg for the LangGraph saver) needed the TLS
setting in libpq form; one shared helper (`plain_dsn`) now builds those connection strings.

### D15. Instance size and cost

**Decision.** `m6i.large` (2 vCPU, 8 GB) with a 30 GB disk. With the database and Temporal elsewhere, the app uses
about 1.5 GB at rest; voice calls need steady CPU, so burstable `t` instances were ruled out (CPU credits run out
mid-call), and ARM instances were ruled out because the image is built and tested on x86.

**Cost.** Roughly $35 to $45 for two weeks running continuously, including disk and the public IP; stopped, only
the disk and IP are paid.

### D16. Safe defaults for a public deployment

- Settings refuse to start outside development with weak secrets (shorter than 16 characters).
- Secrets live only in the server's `.env` (owner-only permissions), never in logs, git or the image; private
  keys are ignored by git and Docker.
- The server's voice agent registers under its own name, so LiveKit never sends a live call to a developer's
  machine.
- Only ports 80 and 443 are public; Postgres and Redis listen on localhost.
- Earlier public addresses redirect to the domain, so links already shared keep working.
