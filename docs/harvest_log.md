# Harvest log

Proven code taken from the earlier Reflexo codebase ([reflexo_code](https://github.com/taha-sadikot-m/reflexo_code)) goes through four steps: copy → adapt → harden → test. Every harvested file gets one entry here. A file can only be used after it meets all eight points of the quality bar.

**Quality bar:**
1. The new module boundaries and names, with no old-architecture references.
2. Fully typed.
3. Idempotent handlers.
4. Masked structured logs and OTel spans.
5. Registry error codes.
6. Ported tests plus new tests.
7. No restart-critical state held only in memory.
8. No secrets copied.

| # | Source (old repo) | Target | Changes made | Tests | Quality bar 1–8 | Work package |
|---|---|---|---|---|---|---|
| H1 | `mcp_servers/meta_mcp/auth/signature.py` | `gateway/adapters/meta_common.py` (`verify_signature`, `sign`) | Kept the constant-time compare. Missing secret or header now fails closed. Added a `sign()` helper for the Lab and tests. Instagram can use a separate app secret | `test_gateway_adapters.py::TestSignatures` (ported + 6 new) | 1–8 ✅ | WP-03 |
| H2 | `mcp_servers/meta_mcp/webhooks/whatsapp.py`, `webhooks/handler.py` | `gateway/adapters/meta_whatsapp.py` (`parse`, `route_key`) | Rewritten as a stateless parser that returns typed `NormalizedEvent`s. No Redis publish and no business lookup inside the parser (routing goes through `connections.find_connection`). Added statuses, `calls` signals, location, contacts, template buttons, list replies | `TestWhatsAppParse` (ported `extract_*` cases + 9 new) | 1–8 ✅ | WP-03 |
| H3 | `mcp_servers/meta_mcp/webhooks/facebook.py`, `webhooks/instagram.py` | `gateway/adapters/meta_messaging.py` | One shared Messenger Platform parser for both channels. Added echo detection (`app_id`, used for human takeover), postbacks, quick replies, attachments, Page feed comments and IG comments, with own comments filtered out | `TestMessagingParse` (ported comment filter + IG DM; 8 new) | 1–8 ✅ | WP-03 |
| H4 | `mcp_servers/meta_mcp/tools/whatsapp.py`, `tools/facebook.py`, `tools/instagram.py` | `gateway/adapters/*::to_payloads`, `gateway/manifests.py`, `gateway/render.py` | Interactive limits (3 buttons, 20-char titles, 10 list rows, 24-char row titles) now live in capability manifests. Two-stage render (blocks → IR → payload) with deterministic degradation. Option maps let a typed reply ("2", "do", "दो", "half kg please") map to an option | `TestPayloads`, `test_gateway_render.py` (ported payload-limit cases + 40 new) | 1–8 ✅ | WP-03 |
| H8 | `mcp_servers/meta_mcp/utils/graph_client.py` | `gateway/adapters/meta_common.py` (`classify_response`, `post_json`) | Error codes are mapped to registry codes (`CREDENTIAL_INVALID`, `WINDOW_CLOSED`, `RATE_LIMITED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`). A read timeout is classified as an *unknown outcome* (not a failure), so the pipeline's wait-then-retry-once rule applies. `Retry-After` is honoured. Token refresh state is not copied (POC uses env tokens; vault in production) | `TestSendClassification` (ported token-error cases + 7 new) | 1–8 ✅ | WP-03 |
| H15 | `tests/test_meta_signature.py`, `test_meta_webhook_parsers.py`, `test_meta_send_payloads.py`, `test_meta_token_errors.py` | `tests/unit/test_gateway_adapters.py`, `tests/fixtures/meta/*.json` | Assertions ported to the new adapter API. The `publish_event`/`resolve_business` mocks are dropped because parsing is pure. 22 realistic webhook fixtures, reused by the Simulation Lab | 62 unit + 21 integration (`test_gateway.py`) | 1–8 ✅ | WP-03 |
