# Quality

An AI product is only as good as what it does when the model is wrong, a provider retries, a process dies or a
customer types something unexpected. Quality here is enforced in five layers, and the system was driven end to end
like a real user before every release.

## 1. Static checks (every change)

| Check | Tool |
|---|---|
| Lint and format | ruff |
| Types | mypy `--strict` over the whole package |
| Module boundaries | `scripts/check_boundaries.py`: a module may import another only through its `api.py`, and only along allowed edges |
| Forbidden patterns | `scripts/check_guards.py`: no `eval`/`exec`, no legacy names |
| Web app | TypeScript type check and production build |

## 2. Tests: 612 passing

| Layer | What it covers |
|---|---|
| Unit (24 files) | Output gate rules, grounding, language detection and policy, rendering per channel, playbook conditions, automation language and read-backs, import parsing, voice gating, house style |
| Integration (27 files) | Real Postgres (testcontainers) with row-level security, real Temporal test server, fake model provider: the full pipeline from signed webhook to sent message, turns and supersession, orders and payments, the learning loop, ask-the-owner, imports, automations, voice bridge and in-call events, admin and owner APIs, sign-up, the public demo |
| Live (on demand) | Selected flows against the real model and providers |

Schema guards run as tests: every table has row-level security forced, every table with a `business_id` has a
tenant policy, the app role without a tenant sees no rows, and the Postgres enums match the Python enums.

## 3. Evaluations against the real model

Eight suites run through the deployed pipeline (signed simulator webhooks into the real ingress, the real model,
the real gate) and read the outcome back from the X-ray. Each has a pass threshold; a release below threshold does
not ship.

| Suite | What it measures | Threshold | Latest result |
|---|---|---|---|
| `tool_selection` | The customer desk calls the right tools and not the wrong ones | 85% | 40/40 (100%) |
| `gate_adversarial` | Invented prices, discounts and links are blocked | 100% | 38/38 (100%) |
| `grounding` | Real answers are not falsely blocked | 90% | 20/20 (100%) |
| `language` | Every reply is in the platform language (English), including to Hindi and Hinglish messages | 90% | 20/20 (100%) |
| `ask_owner_scope` | Owner answers are saved for the right audience (this customer vs everyone) | 80% | 15/15 (100%) |
| `owner_commands` | Plain-language owner instructions become the right changes | 85% | 14/15 (93%) |
| `builder` | Owner descriptions become valid automations | 7/8 | 8/8 (100%) |
| `learning` | Decisions become correct rules; rules decide later cases | 80% | 8/8 (100%) |

Each run writes a report to `evals/reports/<commit>.md` with per-case reasons and p50/p95 latency.

## 4. Chaos lab

From the ops console, each action breaks something on purpose using a simulated customer through the real
pipeline, and the verdict is computed from the database afterwards, not asserted: duplicate webhooks (deduplicated),
a burst of messages (coalesced into one answer), killing the worker mid-turn (resumed, sent once), a slow model
(holding reply, then the answer), Redis down (the system keeps working), prompt injection (refused), and a
cross-business read (blocked by row-level security).

## 5. End-to-end runs like a real user

Every feature was driven in a real browser (Playwright, Edge, fake microphone) on the local stack and then on the
live server: landing, demo chat, payment, voice call with a photo sent mid-call, owner decisions, sign-up and setup
by chat, every ops page. Timings were recorded (live: chat replies 4.3 to 8.6 s, voice ready in 3.9 to 6.3 s).

## Root-cause log

Defects found by driving the product, each fixed at its cause with a regression test. The rule: never patch the
example (no phrase lists, no special cases).

| Symptom | Root cause | Fix |
|---|---|---|
| A chat message sent as the caller hung up was never answered | It was routed into a call that was ending, and nothing returned unanswered call messages to chat; the in-call handler also crashed on a reserved logging keyword | The call ends the moment the caller leaves; routing locks the call row; unanswered messages go back to the chat AI; logging fixed |
| A Hinglish customer suddenly got English replies | The detector called a mixed message English whenever Hindi evidence was weak, ignoring the customer's established language | Weigh Hindi and English function words; when unclear, keep the established language |
| The voice agent promised "I'll check with the owner" but no one was asked | The voice agent had no tool to ask, and nothing checked promises against facts | Ask-the-owner is a voice tool; at hang-up, promises are reconciled against what happened and unbacked ones become owner questions |
| A typed menu was ignored during set-up | Imports only accepted files; the new tool was missing from the desk's bound tool list | Typed lists are an import source with price verification; a test keeps tagged tools and bound lists equal |
| Every onboarding preview answer said "I'd check with you first" | The model omitted one required schema field, so correct answers were rejected twice and fell back | The schema infers the field when the content is unambiguous; still rejects when it is not |
| Correct prices were blocked by the gate | Grounding ignored price modifiers (eggless +₹50) and facts returned by tools in the same turn | Grounding includes modifiers and this turn's tool results |
| The AI stayed silent forever after the owner replied once from the dashboard | "Reply as team" started a human takeover with no way back | The owner is alerted and can hand back to the AI; waiting messages get answered |
| A page of the dashboard went blank | An effect returned a Promise, which React called as a cleanup | Effects never return values; every page sits in an error boundary |
| The chats page could not scroll | The shared card wrapped content in an element with no height, so lists were clipped instead of scrolling | The card is a flex column whose body fills it; fixed for every page using it |
| A photo sent in the demo showed as "[image]" | Media links were signed with the public host, which the browser could not load in every setup | Our own media is served same-origin; the placeholder hides behind the real image |
| Production build failed with "tsc: not found" | npm crashed during install but exited 0, because the registry setting was not copied into the build | The build uses the project's npm config and fails loudly if the toolchain is missing |
| A managed database rejected direct connections | Drivers used directly needed the TLS setting in libpq form | One helper builds those connection strings; unit-tested |
| A live call could reach a developer's machine | The server and a dev machine registered the same LiveKit agent name | Each deployment registers its own agent name |
| Chat replies took 65 s on the first deploy | The database was 180 ms away and a reply makes a few hundred queries | Database on the same machine (see [DEPLOYMENT.md](DEPLOYMENT.md)); rule: never across regions |
