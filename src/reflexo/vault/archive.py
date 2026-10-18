"""The encrypted legal archive (BETA_10 §3.2): built in WP-06 with the voice-note archive tables.

It uses its own key ring (`REFLEXO_ARCHIVE_KEYS`), never the vault keys, and nothing in the product can read it:
objects are opened only by `scripts/archive_open.py` with a reason, two approvers and a ticket.
"""

from __future__ import annotations
