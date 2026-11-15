import json
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import sleep

from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.provider import CompatibleHttpProvider
from shared.models.ai import ProviderCapture, ProviderMessage, ProviderProfile
from shared.models.common import SecretInput


class HttpProviderTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.delay = 0
        self.body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ).encode("utf-8")
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                test.requests.append((self.path, dict(self.headers), body))
                status, response, delay = test.status, test.body, test.delay
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    if 300 <= status < 400:
                        self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    sleep(delay)
                    self.wfile.write(response)
                except OSError:
                    pass  # Expected when the client rejects or times out.

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.close_server)
        port = self.server.server_address[1]
        self.capture = ProviderCapture(
            ProviderProfile(
                "card",
                "owner",
                "Local test",
                f"http://127.0.0.1:{port}/v1/chat/completions?test=1",
                "test-model",
                "chat-completions-compatible",
                1,
                True,
            ),
            SecretInput("FAKE-HTTP-KEY"),
        )
        self.messages = (ProviderMessage("user", "Hello café"),)
        self.provider = CompatibleHttpProvider(connect_timeout=1, read_timeout=1)

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def reject(self, code, call):
        with self.assertRaises(ApplicationError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("FAKE-HTTP-KEY", str(caught.exception))

    def complete(self):
        return self.provider.complete(self.messages, self.capture)

    def test_request_and_response_round_trip(self):
        result = self.complete()
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual((result.input_tokens, result.output_tokens), (3, 1))
        self.assertEqual(len(self.requests), 1)
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions?test=1")
        self.assertEqual(headers["Authorization"], "Bearer FAKE-HTTP-KEY")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(
            json.loads(body),
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello café"}],
                "stream": False,
            },
        )
        self.assertNotIn("FAKE-HTTP-KEY", body.decode("utf-8"))

    def test_http_errors_do_not_redirect_retry_or_expose_body(self):
        for status in (302, 307, 401, 429, 503):
            with self.subTest(status=status):
                self.status = status
                self.body = b"FAKE-HTTP-KEY"
                before = len(self.requests)
                code = ErrorCode.PROVIDER_UNCERTAIN if status >= 500 else ErrorCode.PROVIDER
                self.reject(code, self.complete)
                self.assertEqual(len(self.requests), before + 1)

    def test_invalid_and_oversized_responses(self):
        invalid = (
            b"not JSON",
            b'{"choices":[]}',
            b'{"choices":[],"choices":[]}',
            b'{"choices":[{"message":{"role":"assistant","content":null}}]}',
            b"\xff",
            b"x" * (1024 * 1024 + 1),
        )
        for body in invalid:
            with self.subTest(size=len(body)):
                self.body = body
                before = len(self.requests)
                self.reject(ErrorCode.PROVIDER, self.complete)
                self.assertEqual(len(self.requests), before + 1)

    def test_optional_usage_and_invalid_token_count(self):
        response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "length",
                }
            ]
        }
        self.body = json.dumps(response).encode()
        result = self.complete()
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertEqual(result.finish_reason, "length")
        response["usage"] = {"prompt_tokens": True}
        self.body = json.dumps(response).encode()
        self.reject(ErrorCode.PROVIDER, self.complete)

    def test_read_timeout_is_uncertain_and_not_retried(self):
        self.delay = 0.2
        self.provider = CompatibleHttpProvider(connect_timeout=1, read_timeout=0.05)
        self.reject(ErrorCode.PROVIDER_UNCERTAIN, self.complete)
        self.assertEqual(len(self.requests), 1)

    def test_invalid_requests_are_rejected_before_sending(self):
        self.reject(
            ErrorCode.INVALID,
            lambda: self.provider.complete((), self.capture),
        )
        for endpoint in (
            "http://example.test/chat",
            "file:///tmp/model",
            "https://user:password@example.test/chat",
        ):
            capture = replace(
                self.capture,
                profile=replace(self.capture.profile, endpoint=endpoint),
            )
            self.reject(
                ErrorCode.INVALID,
                lambda: self.provider.complete(self.messages, capture),
            )
        capture = replace(
            self.capture,
            credential=SecretInput("FAKE-HTTP-KEY\r\nInjected: yes"),
        )
        self.reject(
            ErrorCode.INVALID,
            lambda: self.provider.complete(self.messages, capture),
        )
        self.assertEqual(self.requests, [])
