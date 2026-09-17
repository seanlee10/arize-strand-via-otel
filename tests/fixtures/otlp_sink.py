"""Mock upstream for Collector integration tests; stores only synthetic test data."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

seen = set()
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        space = self.headers.get('arize-space-id')
        with lock:
            retry = space not in seen
            seen.add(space)
            print(json.dumps({
                'space': space,
                'authorization': self.headers.get('authorization'),
                'path': self.path,
                'retry': retry,
                'body': base64.b64encode(body).decode(),
            }), flush=True)
        self.send_response(503 if retry else 200)
        self.send_header('Content-Type', 'application/x-protobuf')
        self.end_headers()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
