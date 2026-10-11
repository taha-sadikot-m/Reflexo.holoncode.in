"""Process bootstrap: the only module allowed to import every other module (POC_01 §4.2).

Each module may expose `register(registry)` in its `api.py` to provide port implementations.
"""

from __future__ import annotations

import importlib

from reflexo.common.config import get_settings
from reflexo.common.logging import configure_logging, get_logger
from reflexo.common.ports import Registry, reset_registry
from reflexo.common.tracing import configure_tracing

MODULES = (
    "pipeline",
    "gateway",
    "agent",
    "voice",
    "knowledge",
    "commerce",
    "automations",
    "integrations",
    "approvals",
    "owner",
    "admin",
    "workflows",
)

log = get_logger(__name__)


def bootstrap(service: str) -> Registry:
    settings = get_settings()
    configure_logging(service, settings.log_level)
    configure_tracing(service, settings.otel_exporter_otlp_endpoint, settings.env, settings.otel_sampling_ratio)
    registry = reset_registry()
    for name in MODULES:
        module = importlib.import_module(f"reflexo.{name}.api")
        register = getattr(module, "register", None)
        if callable(register):
            register(registry)
    registry.verify()
    log.info("bootstrap_complete", service=service, env=settings.env)
    return registry
