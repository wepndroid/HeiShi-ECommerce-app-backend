"""Narrow local proxy used only for temporary PayPal Sandbox webhook testing."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8002
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8001
WEBHOOK_PATH = "/v1/payments/webhooks/paypal"
MAX_BODY_BYTES = 1_000_000


class PayPalWebhookProxy(BaseHTTPRequestHandler):
    server_version = "HeyMarketPayPalWebhookProxy/1.0"

    def do_POST(self) -> None:
        if self.path != WEBHOOK_PATH:
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_error(413)
            return

        body = self.rfile.read(content_length)
        forwarded_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length", "transfer-encoding"}
        }
        forwarded_headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=30)
        try:
            connection.request("POST", WEBHOOK_PATH, body=body, headers=forwarded_headers)
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status)
            content_type = response.getheader("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except OSError:
            self.send_error(502)
        finally:
            connection.close()

    def do_GET(self) -> None:
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), PayPalWebhookProxy).serve_forever()
