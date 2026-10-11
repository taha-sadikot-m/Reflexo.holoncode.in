"""Public interface of the `admin` module. Other modules import only from here (POC_01 §4).

Admin API, X-ray stream, Simulation Lab scenarios, chaos endpoints, evals. Spec: POC_07, POC_08 §4.
"""

from reflexo.admin.actions import reply_as_team, set_takeover
from reflexo.admin.auth import COOKIE, CSRF_HEADER, Session, check_credentials
from reflexo.admin.auth import issue as issue_session
from reflexo.admin.auth import record_failure as record_login_failure
from reflexo.admin.auth import throttled as login_throttled
from reflexo.admin.auth import verify as verify_session
from reflexo.admin.stream import xray_stream
from reflexo.admin.views import businesses, conversation_thread, conversations, health, trace_detail, traces

__all__ = [
    "COOKIE",
    "CSRF_HEADER",
    "Session",
    "businesses",
    "check_credentials",
    "conversation_thread",
    "conversations",
    "health",
    "issue_session",
    "login_throttled",
    "record_login_failure",
    "reply_as_team",
    "set_takeover",
    "trace_detail",
    "traces",
    "verify_session",
    "xray_stream",
]
