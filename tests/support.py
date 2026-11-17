import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def telegram_server(responses=None):
    """Real loopback HTTP transport; never contacts Telegram or uses real tokens."""
    received = []
    replies = list(responses or [])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "payload": payload})
            reply = (
                replies.pop(0) if replies else {"ok": True, "result": {"message_id": len(received)}}
            )
            raw = json.dumps(reply).encode()
            self.send_response(200 if reply.get("ok") else reply.get("error_code", 400))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
