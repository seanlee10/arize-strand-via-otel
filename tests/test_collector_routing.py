"""Opt-in Docker test: RUN_COLLECTOR_TESTS=1 uv run python -m unittest discover -s tests -v."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@unittest.skipUnless(os.getenv('RUN_COLLECTOR_TESTS') == '1', 'Set RUN_COLLECTOR_TESTS=1 for Docker integration')
class CollectorRoutingTest(unittest.TestCase):
    def test_concurrent_spaces_retries_and_missing_headers(self):
        suffix = uuid.uuid4().hex[:10]
        network, sink, collector = (f'routing-{part}-{suffix}' for part in ('net', 'sink', 'collector'))
        docker('network', 'create', network)
        try:
            docker('run', '-d', '--name', sink, '--network', network,
                   '--network-alias', 'sink', '-v', f'{ROOT / "tests/fixtures/otlp_sink.py"}:/sink.py:ro',
                   'python:3.11-alpine', 'python', '-u', '/sink.py')
            docker('run', '-d', '--name', collector, '--network', network,
                   '-p', '127.0.0.1::4318', '-p', '127.0.0.1::13133',
                   '-v', f'{ROOT / "otel-collector.yaml"}:/config.yaml:ro',
                   '-e', 'ARIZE_API_KEY=collector-test-key',
                   '-e', 'ARIZE_COLLECTOR_ENDPOINT=http://sink:8080/v1/traces',
                   'otel/opentelemetry-collector-contrib:0.160.0', '--config=/config.yaml')
            ports = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', collector))
            endpoint = f'http://127.0.0.1:{ports["4318/tcp"][0]["HostPort"]}/v1/traces'
            health = f'http://127.0.0.1:{ports["13133/tcp"][0]["HostPort"]}/'
            deadline = time.monotonic() + 20
            while True:
                try:
                    with urlopen(health, timeout=1):
                        break
                except (URLError, OSError):
                    if time.monotonic() > deadline:
                        self.fail('Collector did not start: ' + docker('logs', collector))
                    time.sleep(0.2)

            def send(index, space):
                request = ExportTraceServiceRequest()
                resource = request.resource_spans.add()
                attr = resource.resource.attributes.add()
                attr.key, attr.value.string_value = 'openinference.project.name', 'routing-test'
                span = resource.scope_spans.add().spans.add()
                span.name = f'{space or "missing"}:{index}'
                span.trace_id = uuid.uuid4().bytes
                span.span_id = os.urandom(8)
                span.start_time_unix_nano = time.time_ns()
                span.end_time_unix_nano = span.start_time_unix_nano + 1000
                # Spoofing a payload attribute must not override a transport header,
                # nor rescue an absent/invalid header.
                attr = span.attributes.add()
                attr.key, attr.value.string_value = 'arize.space_id', 'spoofed'
                headers = {'Content-Type': 'application/x-protobuf', 'authorization': 'untrusted-client-key'}
                if space is not None:
                    headers['arize-space-id'] = space
                with urlopen(Request(endpoint, data=request.SerializeToString(), headers=headers), timeout=10) as response:
                    self.assertEqual(response.status, 200)

            spaces = ['U3BhY2U6QQ==', 'U3BhY2U6Qg==']
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(send, i, space) for i in range(12) for space in spaces]
                futures += [pool.submit(send, 99, space) for space in (None, '', 'invalid space')]
                for future in futures:
                    future.result()

            def records():
                return [json.loads(line) for line in docker('logs', sink).splitlines() if line.startswith('{')]

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                received = records()
                successful = []
                for item in received:
                    payload = ExportTraceServiceRequest.FromString(base64.b64decode(item['body']))
                    for resource in payload.resource_spans:
                        for scope in resource.scope_spans:
                            for span in scope.spans:
                                self.assertIn(item['space'], spaces)
                                self.assertEqual(item['authorization'], 'collector-test-key')
                                self.assertEqual(item['path'], '/v1/traces')
                                self.assertTrue(span.name.startswith(item['space'] + ':'), 'Cross-space batch!')
                                attrs = {a.key: a.value.string_value for a in span.attributes}
                                self.assertEqual(attrs['arize.space_id'], item['space'])
                                if not item['retry']:
                                    successful.append(span.name)
                if len(set(successful)) == 24:
                    break
                time.sleep(0.2)
            self.assertEqual(len(successful), 24, docker('logs', collector))
            self.assertEqual(len(set(successful)), 24)
            self.assertEqual({r['space'] for r in received if r['retry']}, set(spaces))
            # Missing/empty/malformed headers have no route and never hit upstream.
            time.sleep(1.2)
            self.assertEqual(len(records()), len(received))
        finally:
            for name in (collector, sink):
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
