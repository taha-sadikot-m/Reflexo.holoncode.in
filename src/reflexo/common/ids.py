"""UUIDv7 identifiers (time-ordered), generated in the application (POC_01 §6).

Python 3.12 has no stdlib uuid7, so this implements RFC 9562 §5.7 with a per-process monotonic counter:
within the same millisecond, the 12-bit `rand_a` field is incremented so ids stay strictly increasing.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_seq = 0


def new_id() -> uuid.UUID:
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _seq = secrets.randbits(11)  # leave headroom so increments rarely overflow
        else:
            ms = _last_ms
            _seq += 1
            if _seq > 0xFFF:  # counter exhausted within one ms: move to the next ms
                _last_ms += 1
                ms = _last_ms
                _seq = secrets.randbits(11)
        rand_a = _seq
    rand_b = secrets.randbits(62)
    value = (ms & 0xFFFF_FFFF_FFFF) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def id_timestamp_ms(value: uuid.UUID) -> int:
    """Unix milliseconds embedded in a UUIDv7."""
    return value.int >> 80
