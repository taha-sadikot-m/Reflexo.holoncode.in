"""Public interface of the `admin` module. Other modules import only from here (POC_01 §4).

Admin API, X-ray stream, Simulation Lab scenarios, chaos endpoints, evals. Spec: POC_07, POC_08 §4.
"""

from reflexo.admin import accounts, chaos, demo, evals, scenarios, sim, workspace
from reflexo.admin.access import log_access
from reflexo.admin.actions import demo_reset, reply_as_team, set_takeover
from reflexo.admin.auth import (
    COOKIE,
    CSRF_HEADER,
    OWNER_COOKIE,
    SURFACE_HEADER,
    Session,
    check_credentials,
    check_reviewer,
    check_totp,
    reviewer_slugs,
)
from reflexo.admin.auth import issue as issue_session
from reflexo.admin.auth import record_failure as record_login_failure
from reflexo.admin.auth import throttled as login_throttled
from reflexo.admin.auth import verify as verify_session
from reflexo.admin.chaos import recent as recent_chaos
from reflexo.admin.stream import xray_stream
from reflexo.admin.views import businesses, conversation_thread, conversations, health, trace_detail, traces

__all__ = [
    "COOKIE",
    "CSRF_HEADER",
    "OWNER_COOKIE",
    "SURFACE_HEADER",
    "Session",
    "accounts",
    "businesses",
    "chaos",
    "check_credentials",
    "check_reviewer",
    "check_totp",
    "conversation_thread",
    "conversations",
    "demo",
    "demo_reset",
    "evals",
    "health",
    "issue_session",
    "log_access",
    "login_throttled",
    "recent_chaos",
    "record_login_failure",
    "reply_as_team",
    "reviewer_slugs",
    "scenarios",
    "set_takeover",
    "sim",
    "trace_detail",
    "traces",
    "verify_session",
    "workspace",
    "xray_stream",
]
