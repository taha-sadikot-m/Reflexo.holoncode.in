# Live, review, Lab, eval and bench checks (BETA_08 §2.4)

Each entry: the check, when it was run, on what, and the result. The traceability check (CHK-Q-1) requires an
entry for every live, review or Lab check of a delivered work package, and for eval and bench checks that have no
marked test.

| Check | Work package | Date | How | Result |
|---|---|---|---|---|
| CHK-MSG-1 | WP-26 | 2026-10-18 | `python scripts/eval.py messages` on the local stack: 180 situations (9 purposes × 20; English, Hinglish, Hindi; unusual context), real Gemini, role `messages` = gemini-2.5-flash with gemini-2.5-flash-lite as fallback | 180/180 (100%), and 160/160 twice before the ninth purpose was added. gemini-2.5-flash-lite alone: 141/160 (88%), so it is not the role's model |
