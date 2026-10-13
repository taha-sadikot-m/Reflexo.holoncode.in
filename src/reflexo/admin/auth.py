"""Admin session auth (POC_07 §1): signed httpOnly cookie (8 h) + CSRF token for mutating requests.

Token format: base64url(json{u, exp, csrf, b?}) + "." + hex(HMAC-SHA256). Stateless, so any API replica can verify
it; logout clears the cookie. Login attempts are throttled per client address.

Two kinds of session share the format: the operator's (`b` absent: every business, engineering tools) and a shop
owner's (`b` = their business id: that business only, no engineering tools). Owners use their own cookie, so an
operator and an owner can be signed in side by side in one browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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

    @property
    def is_operator(self) -> bool:
        return self.business_id is None


def _sign(payload: bytes) -> str:
    key = get_settings().admin_session_secret.get_secret_value().encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue(user: str, *, business_id: str | None = None, hours: int | None = None) -> tuple[str, Session]:
    s = Session(
        user=user,
        csrf=secrets.token_urlsafe(24),
        expires_at=int(time.time()) + (hours or get_settings().admin_session_hours) * 3600,
        business_id=business_id,
    )
    data: dict[str, object] = {"u": s.user, "exp": s.expires_at, "csrf": s.csrf}
    if business_id is not None:
        data["b"] = business_id
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
        user=str(data["u"]), csrf=str(data["csrf"]), expires_at=int(data["exp"]), business_id=str(b) if b else None
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
