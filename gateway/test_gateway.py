#!/usr/bin/env python3

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gateway.gateway import GatewayConfig, GatewayServer, load_api_key


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    last_authorization: str | None = None

    def do_GET(self) -> None:
        type(self).last_authorization = self.headers.get("Authorization")
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            self._json(200, {"data": [{"id": "deepseek-v4-flash-0731"}]})
            return
        if self.path == "/v1/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            time.sleep(0.02)
            self.wfile.write(b"data: two\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        type(self).last_authorization = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self._json(200, {"model": payload["model"], "ok": True})

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True
        )
        cls.upstream_thread.start()
        cls.key = "test-key-that-is-long-enough"
        cls.gateway = GatewayServer(
            GatewayConfig(
                listen_host="127.0.0.1",
                listen_port=free_port(),
                upstream_url=f"http://127.0.0.1:{cls.upstream.server_port}",
                api_key=cls.key,
                upstream_timeout_seconds=5,
            )
        )
        cls.gateway_thread = threading.Thread(
            target=cls.gateway.serve_forever, daemon=True
        )
        cls.gateway_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        authorized: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.gateway.server_port, timeout=5
        )
        headers: dict[str, str] = {}
        if authorized:
            headers["Authorization"] = f"Bearer {self.key}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
        connection.close()
        return result

    def test_health_and_readiness_do_not_require_credentials(self) -> None:
        self.assertEqual(self.request("GET", "/healthz", authorized=False)[0], 200)
        self.assertEqual(self.request("GET", "/readyz", authorized=False)[0], 200)

    def test_missing_credentials_fail_closed(self) -> None:
        status, headers, body = self.request("GET", "/v1/models", authorized=False)
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Bearer")
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    def test_non_api_paths_are_not_proxied(self) -> None:
        self.assertEqual(self.request("GET", "/admin")[0], 404)

    def test_authenticated_json_request_is_proxied_without_upstream_key(self) -> None:
        payload = json.dumps({"model": "deepseek-v4-flash-0731"}).encode()
        status, _headers, body = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"model": "deepseek-v4-flash-0731", "ok": True},
        )
        self.assertIsNone(UpstreamHandler.last_authorization)

    def test_streaming_response_is_relayed(self) -> None:
        status, headers, body = self.request("GET", "/v1/stream")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(body, b"data: one\n\ndata: two\n\n")

    def test_key_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "api-key"
            key_path.write_text(self.key, encoding="utf-8")
            os.chmod(key_path, 0o644)
            with self.assertRaisesRegex(ValueError, "group/world"):
                load_api_key(key_path)
            os.chmod(key_path, 0o600)
            self.assertEqual(load_api_key(key_path), self.key)


if __name__ == "__main__":
    unittest.main()
