"""Admin session auth (POC_07 §1): signed httpOnly cookie (8 h) + CSRF token for mutating requests.

Token format: base64url(json{u, exp, csrf, b?}) + "." + hex(HMAC-SHA256). Stateless, so any API replica can verify
it; logout clears the cookie. Login attempts are throttled per client address.

Two kinds of session share the format: the operator's (`b` absent: every business, engineering tools) and a shop
owner's (`b` = their business id: that business only, no engineering tools). Owners use their own cookie, so an
operator and an owner can be signed in side by side in one browser. A reviewer is an operator session with `ro` set:
it sees the console for the showcase businesses only, and every mutating request is refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass

from reflexo.common.config import get_settings

COOKIE = "reflexo_admin"
OWNER_COOKIE = "reflexo_owner"
SURFACE_HEADER = "x-reflexo-surface"  # "app": the owner's app or the demo (owner cookie); else the ops console
CSRF_HEADER = "x-csrf-token"
_MAX_ATTEMPTS = 8
_WINDOW_S = 300
_attempts: dict[str, deque[float]] = {}


@dataclass(frozen=True)
class Session:
    user: str
    csrf: str
    expires_at: int
    business_id: str | None = None  # set: an owner session locked to this business
    read_only: bool = False  # a reviewer: looks at everything allowed, changes nothing

    @property
    def is_operator(self) -> bool:
        return self.business_id is None


def _sign(payload: bytes) -> str:
    key = get_settings().admin_session_secret.get_secret_value().encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue(
    user: str, *, business_id: str | None = None, hours: int | None = None, read_only: bool = False
) -> tuple[str, Session]:
    s = Session(
        user=user,
        csrf=secrets.token_urlsafe(24),
        expires_at=int(time.time()) + (hours or get_settings().admin_session_hours) * 3600,
        business_id=business_id,
        read_only=read_only,
    )
    data: dict[str, object] = {"u": s.user, "exp": s.expires_at, "csrf": s.csrf}
    if business_id is not None:
        data["b"] = business_id
    if read_only:
        data["ro"] = 1
    payload = base64.urlsafe_b64encode(json.dumps(data).encode())
    return f"{payload.decode()}.{_sign(payload)}", s


def verify(token: str | None) -> Session | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    b = data.get("b")
    return Session(
        user=str(data["u"]),
        csrf=str(data["csrf"]),
        expires_at=int(data["exp"]),
        business_id=str(b) if b else None,
        read_only=bool(data.get("ro")),
    )


def throttled(client: str) -> bool:
    now = time.monotonic()
    q = _attempts.setdefault(client, deque())
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    return len(q) >= _MAX_ATTEMPTS


def record_failure(client: str) -> None:
    _attempts.setdefault(client, deque()).append(time.monotonic())


def check_credentials(user: str, password: str) -> bool:
    s = get_settings()
    user_ok = hmac.compare_digest(user.encode(), s.admin_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), s.admin_password.get_secret_value().encode())
    return user_ok and pass_ok


TOTP_STEP_S = 30


def totp(secret_b32: str, at: float | None = None) -> str:
    """The 6-digit time-based code (RFC 6238, SHA-1, 30 s), as authenticator apps show it."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // TOTP_STEP_S)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def check_totp(code: str | None) -> bool:
    """The operator's code, accepting one step of clock drift either way. No secret configured: always False."""
    secret = get_settings().operator_totp_secret.get_secret_value()
    if not secret or not code:
        return False
    now = time.time()
    return any(hmac.compare_digest(code.strip(), totp(secret, now + d * TOTP_STEP_S)) for d in (-1, 0, 1))


def check_reviewer(user: str, password: str) -> bool:
    """The read-only reviewer login; disabled unless both a user and a password are configured."""
    s = get_settings()
    expected = s.reviewer_password.get_secret_value()
    if not s.reviewer_user or not expected:
        return False
    user_ok = hmac.compare_digest(user.encode(), s.reviewer_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), expected.encode())
    return user_ok and pass_ok


def reviewer_slugs() -> frozenset[str]:
    return frozenset(x.strip() for x in get_settings().reviewer_businesses.split(",") if x.strip())
