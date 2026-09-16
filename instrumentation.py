"""Initialize tracing before constructing a Strands agent or model."""

import os
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


def setup_tracing() -> TracerProvider:
    # Standard OTLP to the local Collector; Arize auth belongs to the Collector.
    provider = TracerProvider(resource=Resource.create({
        "service.name": "strands-weather-agent",
        "openinference.project.name": os.getenv("ARIZE_PROJECT_NAME")
        or "strands-agent-sample",
    }))
    # Conversion mutates spans, so it MUST precede the batch exporter.
    provider.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or "http://127.0.0.1:4318/v1/traces",
        headers={},
        timeout=10,
    )))
    trace.set_tracer_provider(provider)
    StrandsTelemetry(tracer_provider=provider)
    return provider
