"""Offline Retry-After parsing and error-path regression tests."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx
from pathlib import Path
from types import SimpleNamespace
import unittest

from retry_after import MAX_RETRY_SECONDS, rate_limit_message, retry_after_seconds


def load_client_module():
    loader = SourceFileLoader(
        "chatgpt_web_retry_after_test",
        str(Path(__file__).resolve().parents[1] / "chatgpt-web"),
    )
    specification = spec_from_loader(loader.name, loader)
    module = module_from_spec(specification)
    loader.exec_module(module)
    return module


class RetryAfterTests(unittest.TestCase):
    def test_numeric_seconds_are_exact_and_bounded(self):
        self.assertEqual(retry_after_seconds("53"), 53)
        self.assertEqual(retry_after_seconds(" 0 "), 0)
        self.assertEqual(retry_after_seconds(str(MAX_RETRY_SECONDS)), MAX_RETRY_SECONDS)
        self.assertIsNone(retry_after_seconds(str(MAX_RETRY_SECONDS + 1)))
        self.assertIsNone(retry_after_seconds("-5"))
        self.assertIsNone(retry_after_seconds("53 seconds"))

    def test_http_date_values_are_interpreted_relative_to_current_time(self):
        now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
        retry_at = format_datetime(now + timedelta(seconds=53), usegmt=True)
        self.assertEqual(retry_after_seconds(retry_at, now), 53)
        self.assertEqual(retry_after_seconds(format_datetime(now, usegmt=True), now), 0)
        self.assertEqual(retry_after_seconds(
            format_datetime(now + timedelta(days=9), usegmt=True), now), None)

    def test_untrusted_or_malformed_header_is_never_echoed(self):
        for raw in (None, "", "not-a-date", "53\r\nX-Injected: 1",
                    "Thu, 01 Jan 1970 00:00:00", "99999999999999999"):
            with self.subTest(raw=raw):
                self.assertEqual(rate_limit_message(raw), "ChatGPT web rate limit reached")
        self.assertEqual(rate_limit_message("53"),
                         "ChatGPT web rate limit reached; retry after 53 seconds")

    def test_regular_http_request_uses_validated_error(self):
        app = load_client_module()
        client = object.__new__(app.ChatGPTWebClient)
        client.headers = {}
        client.http = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(
            status_code=429, headers={"retry-after": "53\r\nX-Injected: 1"}))
        with self.assertRaisesRegex(app.ChatGPTWebError, "^ChatGPT web rate limit reached$"):
            client.request("GET", "https://chatgpt.com/backend-api/some-endpoint")

    def test_loopback_429_reports_bounded_cooldown_without_retry(self):
        """Exercise real HTTP headers and the client request path, not a fake response."""
        app = load_client_module()
        received = []

        class RateLimitedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(429)
                self.send_header("Retry-After", "47")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitedHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        client = object.__new__(app.ChatGPTWebClient)
        client.headers = {}
        try:
            with httpx.Client(timeout=3) as client.http:
                url = f"http://127.0.0.1:{server.server_port}/rate-limited"
                with self.assertRaisesRegex(
                    app.ChatGPTWebError,
                    "^ChatGPT web rate limit reached; retry after 47 seconds$",
                ):
                    client.request("GET", url)
            self.assertEqual(received, ["/rate-limited"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_streaming_submission_uses_same_error(self):
        app = load_client_module()
        client = object.__new__(app.ChatGPTWebClient)
        client.headers = {}
        client.create_sentinel_headers = lambda: {}
        client.create_conversation_body = lambda *args: {"messages": [{"id": "test-id"}]}
        response = SimpleNamespace(status_code=429, headers={"retry-after": "53"})
        client.http = SimpleNamespace(
            stream=lambda *args, **kwargs: contextlib.nullcontext(response))
        with self.assertRaisesRegex(
            app.ChatGPTWebError, "^ChatGPT web rate limit reached; retry after 53 seconds$"
        ):
            client.send_prompt_direct(None, "Hello", [])


if __name__ == "__main__":
    unittest.main()
