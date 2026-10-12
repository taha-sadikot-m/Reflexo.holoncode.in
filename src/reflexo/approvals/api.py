"""Public interface of the `approvals` module. Other modules import only from here (POC_01 §4).

Approval requests, short codes, decisions, Temporal signals. Spec: POC_04 §9, POC_06 §7.2.
"""

from reflexo.approvals.requests import (
    Approval,
    Decision,
    create,
    decide,
    get,
    open_for,
    outcome,
    pending,
    record_outcome,
)

__all__ = ["Approval", "Decision", "create", "decide", "get", "open_for", "outcome", "pending", "record_outcome"]
