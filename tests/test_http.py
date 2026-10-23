import io
import json
import unittest
import urllib.error
from email.message import Message
from types import SimpleNamespace

from ticketforge.integrations import HTTP, RateLimited, RemoteError
from ticketforge.store import Superseded


class HTTPTests(unittest.TestCase):
    def http(self, outcomes, base="https://api.example"):
        calls, sleeps = [], []
        http = HTTP(base, {}, sleeper=sleeps.append)
        def open_request(*args, **kwargs):
            calls.append(args[0].method)
            value = outcomes.pop(0)
            if isinstance(value, Exception): raise value
            return io.BytesIO(json.dumps(value).encode())
        http.opener = SimpleNamespace(open=open_request)
        return http, calls, sleeps

    def error(self, status, body=None, **headers):
        msg = Message()
        for name, value in headers.items(): msg[name] = value
        return urllib.error.HTTPError("https://api.example", status, "error", msg,
                                      io.BytesIO(json.dumps(body or {}).encode()))

    def test_get_retries_transient_failure(self):
        http, calls, waits = self.http([self.error(503), {"ok": True}])
        self.assertEqual(http.call("GET", "/resource"), {"ok": True})
        self.assertEqual(calls, ["GET", "GET"])
        self.assertTrue(waits)

    def test_model_529_can_retry_before_any_tool_executes(self):
        http, calls, _ = self.http([self.error(529), {"content": []}], "https://api.anthropic.com")
        self.assertEqual(http.call("POST", "/v1/messages", {}), {"content": []})
        self.assertEqual(calls, ["POST", "POST"])

    def test_issue_write_and_put_are_never_blindly_retried(self):
        for method in ("POST", "PUT"):
            http, calls, waits = self.http([self.error(503), {"ok": True}])
            with self.assertRaises(RemoteError): http.call(method, "/mutation", {})
            self.assertEqual(calls, [method])
            self.assertEqual(waits, [])

    def test_rate_limit_is_deferred_without_hot_loop(self):
        http, calls, waits = self.http([self.error(403, **{"X-RateLimit-Remaining": "0", "Retry-After": "60"})])
        with self.assertRaises(RateLimited) as value: http.call("GET", "/resource")
        self.assertGreaterEqual(value.exception.retry_after, 60)
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [])

    def test_retry_wait_cancels_on_new_input(self):
        http, calls, _ = self.http([self.error(503), {"ok": True}])
        checks = [0]
        def guard():
            checks[0] += 1
            if checks[0] == 2: raise Superseded("new input")
        with self.assertRaises(Superseded): http.call("GET", "/resource", guard=guard)
        self.assertEqual(len(calls), 1)

    def test_github_secondary_rate_limit_uses_header_or_minute_fallback(self):
        for headers, body, delay in (({"Retry-After": "90"}, {}, 90),
                                      ({}, {"message": "You have exceeded a secondary rate limit."}, 60)):
            error = self.error(403, body=body, **headers)
            http, calls, waits = self.http([error], "https://api.github.com")
            with self.assertRaises(RateLimited) as value:
                http.call("GET", "/resource")
            self.assertEqual(value.exception.retry_after, delay)
            self.assertEqual(calls, ["GET"])
            self.assertFalse(waits)

    def test_github_permission_denied_is_not_a_secondary_limit(self):
        error = self.error(403, body={"message": "Resource not accessible"})
        http, calls, waits = self.http([error], "https://api.github.com")
        with self.assertRaises(RemoteError) as value:
            http.call("GET", "/resource")
        self.assertNotIsInstance(value.exception, RateLimited)
        self.assertEqual(calls, ["GET"])
        self.assertFalse(waits)

    def test_secondary_limit_does_not_repeat_a_write(self):
        http, calls, waits = self.http([self.error(403, **{"Retry-After": "60"})], "https://api.github.com")
        with self.assertRaises(RateLimited):
            http.call("POST", "/mutation", {})
        self.assertEqual(calls, ["POST"])
        self.assertFalse(waits)
