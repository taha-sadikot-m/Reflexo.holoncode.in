"""Admin session auth (POC_07 §1): signed httpOnly cookie (8 h) + CSRF token for mutating requests.

Token format: base64url(json{u, exp, csrf}) + "." + hex(HMAC-SHA256). Stateless, so any API replica can verify
it; logout clears the cookie. Login attempts are throttled per client address.
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
CSRF_HEADER = "x-csrf-token"
_MAX_ATTEMPTS = 8
_WINDOW_S = 300
_attempts: dict[str, deque[float]] = {}


@dataclass(frozen=True)
class Session:
    user: str
    csrf: str
    expires_at: int


def _sign(payload: bytes) -> str:
    key = get_settings().admin_session_secret.get_secret_value().encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue(user: str) -> tuple[str, Session]:
    s = Session(
        user=user,
        csrf=secrets.token_urlsafe(24),
        expires_at=int(time.time()) + get_settings().admin_session_hours * 3600,
    )
    payload = base64.urlsafe_b64encode(json.dumps({"u": s.user, "exp": s.expires_at, "csrf": s.csrf}).encode())
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
    return Session(user=str(data["u"]), csrf=str(data["csrf"]), expires_at=int(data["exp"]))


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
