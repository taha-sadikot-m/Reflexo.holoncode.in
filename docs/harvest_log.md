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
| | | | | | | |
