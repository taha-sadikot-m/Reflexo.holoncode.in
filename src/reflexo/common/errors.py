"""Error code registry and the base error type (POC_01 §6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    OPTED_OUT = "OPTED_OUT"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    GROUNDING_FAILED = "GROUNDING_FAILED"
    OUTPUT_GATE_FAILED = "OUTPUT_GATE_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DUPLICATE = "DUPLICATE"
    LOOP_DETECTED = "LOOP_DETECTED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    AUTOMATION_CAP_REACHED = "AUTOMATION_CAP_REACHED"
    TEMPLATE_REQUIRED = "TEMPLATE_REQUIRED"
    LEASE_LOST = "LEASE_LOST"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    COD_NOT_ALLOWED = "COD_NOT_ALLOWED"
    CAPACITY_FULL = "CAPACITY_FULL"
    INTERNAL = "INTERNAL"


class ReflexoError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        guidance: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.guidance = guidance
        self.details = details or {}

    def to_envelope(self) -> dict[str, Any]:
        """Tool-result error envelope (POC_01 §6)."""
        return {
            "ok": False,
            "error": {
                "code": str(self.code),
                "message": self.message,
                "retryable": self.retryable,
                "guidance": self.guidance,
            },
        }
