"""Exercise the real agent loop and OTLP serialization without external APIs."""

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from agent import create_agent
from instrumentation import setup_tracing
from mock_trace import MockModel


class TracingTest(unittest.TestCase):
    def test_invalid_route_does_not_export_or_inherit_env_credentials(self):
        # An explicit empty value overrides the environment for either half of
        # the route: a bad client must never borrow another tenant's settings.
        for overrides in ({"space_id": ""}, {"space_id": "invalid space"},
                          {"space_id": "bad;multiple"}, {"api_key": ""}, {"api_key": "   "}):
            with self.subTest(**overrides), patch.dict(os.environ, {
                "ARIZE_SPACE_ID": "U3BhY2U6b3RoZXI=",
                "ARIZE_API_KEY": "env-fallback-key",
            }), patch("instrumentation.trace.set_tracer_provider"), patch("instrumentation.StrandsTelemetry"), patch("instrumentation.HTTPSpanExporter") as exporter, patch("instrumentation.GRPCSpanExporter") as grpc_exporter:
                with self.assertLogs("instrumentation", level="WARNING") as logs:
                    provider = setup_tracing(**overrides)
                self.assertNotIn("env-fallback-key", "".join(logs.output))
                try:
                    # No export, including when an explicit empty route overrides env.
                    with provider.get_tracer(__name__).start_as_current_span("still-runs"):
                        pass
                    provider.force_flush()
                    exporter.assert_not_called()
                    grpc_exporter.assert_not_called()
                finally:
                    provider.shutdown()

    def test_protocol_env_selects_transport_and_its_endpoint_default(self):
        # A gRPC endpoint is host:port; an HTTP one carries /v1/traces. Mixing
        # them fails silently, so the exporter and the default move together.
        cases = [("grpc", "GRPCSpanExporter", "http://127.0.0.1:4317"),
                 ("http/protobuf", "HTTPSpanExporter", "http://127.0.0.1:4318/v1/traces")]
        for protocol, chosen, endpoint in cases:
            other = "HTTPSpanExporter" if chosen == "GRPCSpanExporter" else "GRPCSpanExporter"
            with self.subTest(protocol=protocol), patch.dict(os.environ, {
                "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
                "ARIZE_SPACE_ID": "U3BhY2U6dGVzdA==",
                "ARIZE_API_KEY": "transport-test-key",
            }, clear=False), patch("instrumentation.trace.set_tracer_provider"), \
                    patch("instrumentation.StrandsTelemetry"), \
                    patch(f"instrumentation.{chosen}") as used, \
                    patch(f"instrumentation.{other}") as unused:
                os.environ.pop("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None)
                provider = setup_tracing()
                try:
                    unused.assert_not_called()
                    used.assert_called_once()
                    kwargs = used.call_args.kwargs
                    self.assertEqual(kwargs["endpoint"], endpoint)
                    # The client always uses the gateway's own header names.
                    self.assertEqual(kwargs["headers"], {
                        "space_id": "U3BhY2U6dGVzdA==",
                        "arize_api_key": "transport-test-key",
                    })
                finally:
                    provider.shutdown()

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
                "ARIZE_SPACE_ID": "U3BhY2U6dGVzdA==",
                "ARIZE_PROJECT_NAME": "local-test-project",
                "ARIZE_COLLECTOR_ENDPOINT": "https://must-not-contact.invalid/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{server.server_port}/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": "none",
                "OTEL_EXPORTER_OTLP_COMPRESSION": "none",
            }):
                provider = setup_tracing()
                try:
                    result = create_agent(MockModel())("서울 날씨를 알려줘.")
                    self.assertIn("22°C", str(result))
                    self.assertTrue(provider.force_flush(timeout_millis=10000))
                finally:
                    provider.shutdown()

            self.assertTrue(requests, "No OTLP export received")
            spans = []
            for path, headers, request in requests:
                self.assertEqual(path, "/v1/traces")
                headers = {key.lower(): value for key, value in headers.items()}
                # Both halves of the route are client-supplied per request,
                # under the gateway's names; the Collector renames them.
                self.assertEqual(headers["arize_api_key"], "local-test-key")
                self.assertEqual(headers["space_id"], "U3BhY2U6dGVzdA==")
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
