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


def setup_tracing(*, space_id: str | None = None, project_name: str | None = None) -> TracerProvider:
    """Configure one immutable destination per client process.

    Missing/invalid routing disables export; agent execution can still proceed.
    Initialize once before creating agents, never mutate headers between calls.
    """
    # Standard OTLP to the local Collector; Arize auth belongs to the Collector.
    provider = TracerProvider(resource=Resource.create({
        "service.name": "strands-weather-agent",
        "openinference.project.name": project_name or os.getenv("ARIZE_PROJECT_NAME")
        or "strands-agent-sample",
    }))
    # Conversion mutates spans, so it MUST precede the batch exporter.
    provider.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
    target = os.getenv("ARIZE_SPACE_ID", "") if space_id is None else space_id
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", target):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or "http://127.0.0.1:4318/v1/traces",
            headers={"arize-space-id": target},
            timeout=10,
        )))
    else:
        logging.getLogger(__name__).warning(
            "Missing or malformed ARIZE_SPACE_ID: traces will not be exported."
        )
    trace.set_tracer_provider(provider)
    StrandsTelemetry(tracer_provider=provider)
    return provider
