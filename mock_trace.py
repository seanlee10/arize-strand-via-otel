"""Send one mock agent trace without calling a model API.

The agent loop, the OpenInference conversion, and the export headers are the
real ones; only the model is replaced. Use this to prove a Collector or an
Arize endpoint accepts traffic without spending model credits.

    uv run mock_trace.py                                   # via local Collector
    uv run mock_trace.py --endpoint https://otlp.arize.com/v1/traces
    uv run mock_trace.py --space-id "$SPACE_B" --project-name probe-b
"""

import argparse
import json
import logging
import os
import sys

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from strands.models.model import Model

from agent import create_agent
from instrumentation import load_config, resolve_route, setup_tracing


class MockModel(Model):
    """Deterministic stand-in for AnthropicModel: one tool call, then an answer."""

    def update_config(self, **model_config):
        pass

    def get_config(self):
        return {"model_id": "mock-model"}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        has_result = any(
            "toolResult" in block
            for message in messages
            for block in message["content"]
        )
        yield {"messageStart": {"role": "assistant"}}
        if not has_result:
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": "weather-1", "name": "get_weather",
            }}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps({"city": "Seoul"}),
            }}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": "샘플 데이터: 서울 22°C."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            "metrics": {"latencyMs": 1},
        }}


class TraceIdCollector(SpanProcessor):
    """Record trace IDs so the caller can look the trace up after export."""

    def __init__(self) -> None:
        self.trace_ids: list[str] = []

    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        trace_id = format(span.context.trace_id, "032x")
        if trace_id not in self.trace_ids:
            self.trace_ids.append(trace_id)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="서울 날씨를 알려줘.")
    parser.add_argument("--space-id", help="Destination Space ID (overrides ARIZE_SPACE_ID)")
    parser.add_argument("--project-name", help="Arize project (overrides ARIZE_PROJECT_NAME)")
    parser.add_argument("--endpoint", help="OTLP traces URL (default: local Collector)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # No model call, so no model credentials are required.
    load_config(required=())
    if args.endpoint:
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = args.endpoint
    _, _, missing = resolve_route(space_id=args.space_id)
    if missing:
        print("Export is not configured: " + ", ".join(missing), file=sys.stderr)
        return 2

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "http://127.0.0.1:4318/v1/traces"
    print(f"Exporting mock trace to {endpoint}")
    provider = setup_tracing(space_id=args.space_id, project_name=args.project_name)
    collector = TraceIdCollector()
    # Appended after conversion: this processor only observes, never mutates.
    provider.add_span_processor(collector)
    try:
        print(create_agent(MockModel())(args.prompt))
        flushed = provider.force_flush(timeout_millis=10000)
    finally:
        provider.shutdown()

    for trace_id in collector.trace_ids:
        print(f"trace_id: {trace_id}")
    if not flushed:
        print("Export did not complete within the timeout.", file=sys.stderr)
        return 1
    # A completed flush alone does not prove that Arize ingested the spans.
    print("Flush completed. Verify ingestion in Arize.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
