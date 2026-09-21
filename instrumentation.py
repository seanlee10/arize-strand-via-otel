"""Initialize tracing before constructing a Strands agent or model."""

import os
import logging
import re
from pathlib import Path

from dotenv import load_dotenv
from openinference.instrumentation.strands_agents import (
    StrandsAgentsToOpenInferenceProcessor,
)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from strands.telemetry import StrandsTelemetry


def load_config() -> None:
    # Existing shell variables take precedence over the project-local .env.
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    required = ("ANTHROPIC_API_KEY",)
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("프로젝트 .env에 설정이 필요합니다: " + ", ".join(missing))


def setup_tracing(
    *,
    space_id: str | None = None,
    project_name: str | None = None,
    api_key: str | None = None,
) -> TracerProvider:
    """Configure one immutable destination and credential per client process.

    Missing/invalid routing disables export; agent execution can still proceed.
    Initialize once before creating agents, never mutate headers between calls.
    """
    # Standard OTLP to the local Collector, which relays the client's own
    # credential upstream and stores none of its own.
    provider = TracerProvider(resource=Resource.create({
        "service.name": "strands-weather-agent",
        "openinference.project.name": project_name or os.getenv("ARIZE_PROJECT_NAME")
        or "strands-agent-sample",
    }))
    # Conversion mutates spans, so it MUST precede the batch exporter.
    provider.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
    # Only None falls back to the environment: an explicit "" is a deliberate
    # override that must not inherit another tenant's destination or key.
    target = os.getenv("ARIZE_SPACE_ID", "") if space_id is None else space_id
    key = (os.getenv("ARIZE_API_KEY", "") if api_key is None else api_key).strip()
    missing = [name for name, valid in (
        ("ARIZE_SPACE_ID", bool(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", target))),
        ("ARIZE_API_KEY", bool(key)),
    ) if not valid]
    if missing:
        # Name the setting only; the credential itself never reaches the logs.
        logging.getLogger(__name__).warning(
            "Missing or malformed %s: traces will not be exported.", ", ".join(missing)
        )
    else:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or "http://127.0.0.1:4318/v1/traces",
            headers={"arize-space-id": target, "authorization": key},
            timeout=10,
        )))
    trace.set_tracer_provider(provider)
    StrandsTelemetry(tracer_provider=provider)
    return provider
