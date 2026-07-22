from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"auth_id": "mock_001", "status": "approved"}).encode())

    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 9090), Handler).serve_forever()
