"""OpenTelemetry setup and trace-context helpers for queue hops (POC_08 §3.1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

_configured = False


class GENAI:
    """OpenTelemetry GenAI semantic-convention attribute names (BETA_12 §7). The conventions are still in
    "Development", so the names are pinned here and changed deliberately. Message content never goes on a span."""

    OPERATION = "gen_ai.operation.name"  # chat | embeddings | generate_content
    PROVIDER = "gen_ai.provider.name"  # gcp.gemini | deepgram | livekit
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"
    INPUT_TOKENS = "gen_ai.usage.input_tokens"
    OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    CACHED_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
    REASONING_TOKENS = "gen_ai.usage.reasoning.output_tokens"
    CONVERSATION_ID = "gen_ai.conversation.id"
    COST_MICRO_INR = "reflexo.cost.micro_inr"
    PURPOSE = "reflexo.purpose"
    PROMPT_VERSION = "reflexo.prompt_version"
    FEATURE = "reflexo.feature"
    ROUTE = "reflexo.route"

    PROVIDER_NAMES: ClassVar[dict[str, str]] = {
        "gemini": "gcp.gemini",
        "deepgram": "deepgram",
        "livekit": "livekit",
        "fake": "fake",
    }


def configure_tracing(service: str, endpoint: str | None, env: str, sampling_ratio: float = 1.0) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "deployment.environment": env}),
        sampler=ParentBasedTraceIdRatio(sampling_ratio),
    )
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("reflexo")


def current_trace_context() -> dict[str, str]:
    """Serialize the active context (W3C traceparent) for storage in a queue row's `trace_context`."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def resume_trace(carrier: dict[str, Any] | None) -> Iterator[None]:
    """Continue the trace stored on a queue row (consumer side of a queue hop)."""
    if not carrier:
        yield
        return
    token = otel_context.attach(extract(carrier))
    try:
        yield
    finally:
        otel_context.detach(token)
