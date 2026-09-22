"""Mock gRPC OTLP upstream; prints request metadata so header names are visible."""
import base64
import json
from concurrent import futures

import grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc


class Service(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):
        # Metadata keys arrive lowercased; values are the forwarded credentials.
        metadata = {key: value for key, value in context.invocation_metadata()
                    if not key.startswith("grpc-")}
        print(json.dumps({
            "metadata": metadata,
            "body": base64.b64encode(request.SerializeToString()).decode(),
        }), flush=True)
        return trace_service_pb2.ExportTraceServiceResponse()


server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), server)
server.add_insecure_port("0.0.0.0:8080")
server.start()
server.wait_for_termination()
