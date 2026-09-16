"""Exercise the real agent loop and OTLP serialization without external APIs."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from strands.models.model import Model

from agent import create_agent
from instrumentation import setup_tracing


class DemoModel(Model):
    def update_config(self, **model_config):
        pass

    def get_config(self):
        return {"model_id": "local-test-model"}

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


class TracingTest(unittest.TestCase):
    def test_agent_tool_and_llm_are_transformed_before_http_export(self):
        requests = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                request = ExportTraceServiceRequest()
                request.ParseFromString(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, dict(self.headers), request))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {
                "ARIZE_API_KEY": "local-test-key",
                "ARIZE_SPACE_ID": "local-test-space",
                "ARIZE_PROJECT_NAME": "local-test-project",
                "ARIZE_COLLECTOR_ENDPOINT": "https://must-not-contact.invalid/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{server.server_port}/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": "none",
                "OTEL_EXPORTER_OTLP_COMPRESSION": "none",
            }):
                provider = setup_tracing()
                try:
                    result = create_agent(DemoModel())("서울 날씨를 알려줘.")
                    self.assertIn("22°C", str(result))
                    self.assertTrue(provider.force_flush(timeout_millis=10000))
                finally:
                    provider.shutdown()

            self.assertTrue(requests, "No OTLP export received")
            spans = []
            for path, headers, request in requests:
                self.assertEqual(path, "/v1/traces")
                headers = {key.lower(): value for key, value in headers.items()}
                self.assertNotIn("authorization", headers)
                self.assertNotIn("arize-space-id", headers)
                for resource in request.resource_spans:
                    attrs = {a.key: a.value.string_value for a in resource.resource.attributes}
                    self.assertEqual(attrs["openinference.project.name"], "local-test-project")
                    for scope in resource.scope_spans:
                        spans.extend(scope.spans)
            attributes = [{a.key: a.value.string_value for a in s.attributes} for s in spans]
            kinds = {a.get("openinference.span.kind") for a in attributes}
            self.assertTrue({"AGENT", "LLM", "TOOL"}.issubset(kinds), kinds)
            for attrs in attributes:
                if attrs.get("openinference.span.kind") in {"AGENT", "LLM", "TOOL"}:
                    self.assertTrue(attrs.get("input.value"), attrs)
                    self.assertTrue(attrs.get("output.value"), attrs)
            tool = next(a for a in attributes if a.get("openinference.span.kind") == "TOOL")
            self.assertIn("22", tool["output.value"])
            self.assertEqual(len({s.trace_id for s in spans}), 1)
            ids = {s.span_id for s in spans}
            self.assertEqual(sum(not s.parent_span_id for s in spans), 1)
            for span in spans:
                if span.parent_span_id:
                    self.assertIn(span.parent_span_id, ids)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
