from __future__ import annotations

import ast
import gzip
import io
import json
import logging
import os
import re
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

import oddsrelay
from oddsrelay import (
    APIError,
    AuthenticationError,
    BadRequestError,
    InsufficientTokensError,
    NotFoundError,
    NotModified,
    OddsRelay,
    PermissionDeniedError,
    QuoteTooHigh,
    RateLimitError,
    ServerError,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
KEY = "test-key-123"  # not a real key: tests never call the API


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def reply(status=200, body=b"", headers=None, gzipped=True):
    msg = Message()
    if body and status != 304:
        msg["Content-Type"] = "text/plain" if body[:1] not in (b"{", b"[") else "application/json"
        if gzipped:
            body = gzip.compress(body)
            msg["Content-Encoding"] = "gzip"
    for k, v in (headers or {}).items():
        msg[k] = v
    return status, msg, body


class FakeResponse(io.BytesIO):
    def __init__(self, status, headers, body):
        super().__init__(body)
        self.status = status
        self.headers = headers


class FakeOpener:
    """Stands in for urllib's opener: hands out canned replies and records each request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        status, headers, body = self.replies.pop(0)
        if status >= 300:  # urllib raises for these, as the real opener does
            raise urllib.error.HTTPError(req.full_url, status, "", headers, io.BytesIO(body))
        return FakeResponse(status, headers, body)


def client(*replies, key=KEY, sleep=None):
    opener = FakeOpener(*replies)
    return OddsRelay(key, opener=opener, sleep=sleep or (lambda s: None)), opener


def query(req):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))


class KeyTests(unittest.TestCase):
    def test_key_from_environment_or_argument(self):
        with mock.patch.dict(os.environ, {"ODDSRELAY_KEY": "env-key"}):
            c, opener = client(reply(body=fixture("v2-sports.page.json")), key=None)
            c.sports()
            c2, opener2 = client(reply(body=fixture("v2-sports.page.json")))
            c2.sports()
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer env-key")
        self.assertEqual(opener2.requests[0].get_header("Authorization"), f"Bearer {KEY}")
        # Sent as an unredirected header, so urllib never forwards it on a redirect.
        self.assertIn("Authorization", opener2.requests[0].unredirected_hdrs)
        self.assertNotIn("Authorization", opener2.requests[0].headers)

    def test_no_key_sends_no_authorization(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            c, opener = client(reply(body=fixture("v2-status.status.json")), key=None)
            c.status()
        self.assertIsNone(opener.requests[0].get_header("Authorization"))
        self.assertEqual(repr(c), "OddsRelay(key=not set)")

    def test_key_never_in_url_repr_or_log(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        old_level, root.level = root.level, logging.DEBUG
        try:
            c, opener = client(
                reply(429, headers={"Retry-After": "1"}, gzipped=False),
                reply(body=fixture("v2-odds-type.quote.json")),
                reply(body=fixture("v2-odds-raw.page.json")),
                reply(401, fixture("error-401.invalid_api_key.json")),
            )
            result = c.raw(region="uk", max_tokens=10)
            with self.assertRaises(AuthenticationError):
                c.usage()
        finally:
            root.removeHandler(handler)
            root.level = old_level
        for req in opener.requests:
            self.assertNotIn(KEY, req.full_url)
        for text in [repr(c), str(c), repr(result)] + [r.getMessage() for r in records]:
            self.assertNotIn(KEY, text)

    def test_key_never_in_exception(self):
        echoed = {"error": {"code": "invalid_api_key", "message": f"bad key {KEY}", "request_id": "or_req_1"}}
        c, _ = client(reply(401, json.dumps(echoed).encode()))
        with self.assertRaises(AuthenticationError) as caught:
            c.usage()
        exc = caught.exception
        for text in (str(exc), repr(exc), exc.message, repr(exc.args), repr(exc.error)):
            self.assertNotIn(KEY, text)


class TransportTests(unittest.TestCase):
    def test_always_asks_for_gzip_and_decompresses(self):
        c, opener = client(
            reply(body=fixture("v2-sports.page.json")),
            reply(body=fixture("v2-sports.page.json"), gzipped=False),
        )
        zipped, plain = c.sports(), c.sports(cursor="next")
        self.assertEqual(opener.requests[0].get_header("Accept-encoding"), "gzip")
        self.assertEqual(zipped.data, json.loads(fixture("v2-sports.page.json")))
        self.assertEqual(plain.data, zipped.data)

    def test_quote_option(self):
        c, opener = client(reply(body=fixture("v2-odds-type.quote.json")))
        result = c.odds("standard", region="uk", quote=True)
        self.assertEqual(query(opener.requests[0])["quote"], "true")
        self.assertEqual(set(result.data), {"cost", "remaining", "rate_card"})

    def test_quote_first_makes_the_call_when_under_max_tokens(self):
        c, opener = client(reply(body=fixture("v2-odds-type.quote.json")), reply(body=fixture("v2-odds-raw.page.json")))
        result = c.raw(region="uk", max_tokens=10)
        self.assertEqual([query(r).get("quote") for r in opener.requests], ["true", None])
        self.assertEqual(result.data["meta"]["feed_type"], "raw")

    def test_quote_first_stops_above_max_tokens(self):
        quote = json.loads(fixture("v2-odds-type.quote.json"))
        quote["cost"] = 25
        c, opener = client(reply(body=json.dumps(quote).encode()))
        with self.assertRaises(QuoteTooHigh) as caught:
            c.odds("standard", region="uk", max_tokens=10)
        self.assertEqual((caught.exception.cost, caught.exception.max_tokens), (25, 10))
        self.assertEqual(len(opener.requests), 1)

    def test_etag_per_url_and_typed_not_modified(self):
        etag = {"ETag": '"c0a1b2c3d-1f4-gz"'}
        c, opener = client(
            reply(body=fixture("v2-sports.page.json"), headers=etag),
            reply(body=fixture("v2-sports.page.json"), headers={"ETag": '"other"'}),
            reply(304, headers={**etag, "X-Tokens-Cost": "0"}),
        )
        first = c.sports()
        c.sports(region="uk")  # another URL: no If-None-Match
        again = c.sports()
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))
        self.assertIsNone(opener.requests[1].get_header("If-none-match"))
        self.assertEqual(opener.requests[2].get_header("If-none-match"), etag["ETag"])
        self.assertIsInstance(again, NotModified)
        self.assertTrue(again.not_modified)
        self.assertFalse(first.not_modified)
        self.assertEqual(again.data, first.data)  # the last body, kept by the client
        self.assertEqual(again.tokens.cost, 0)

    def test_429_waits_retry_after_then_succeeds(self):
        waits = []
        c, opener = client(
            reply(429, headers={"Retry-After": "2"}, gzipped=False),  # the front door's empty-bodied 429
            reply(body=fixture("v2-sports.page.json")),
            sleep=waits.append,
        )
        self.assertEqual(c.sports().status, 200)
        self.assertEqual(waits, [2])
        self.assertEqual(len(opener.requests), 2)

    def test_429_gives_up_after_three_tries(self):
        waits = []
        body = fixture("error-429.too_many_requests.json")
        c, opener = client(
            reply(429, headers={"Retry-After": "3"}, gzipped=False),
            reply(429, headers={"Retry-After": "3"}, gzipped=False),
            reply(429, body, headers={"Retry-After": "4"}),
            sleep=waits.append,
        )
        with self.assertRaises(RateLimitError) as caught:
            c.sports()
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(waits, [3, 3])
        self.assertEqual(caught.exception.code, "too_many_requests")
        self.assertEqual(caught.exception.retry_after, 4)

    def test_empty_429_has_no_code(self):
        c, _ = client(*[reply(429, headers={"Retry-After": "1"}, gzipped=False)] * 3)
        with self.assertRaises(RateLimitError) as caught:
            c.sports()
        self.assertIsNone(caught.exception.code)


class ErrorTests(unittest.TestCase):
    def test_402_is_typed_with_cost_remaining_and_reset(self):
        c, _ = client(reply(402, fixture("error-402.insufficient_tokens.json"), headers={"X-Tokens-Cost": "0"}))
        with self.assertRaises(InsufficientTokensError) as caught:
            c.odds("standard", region="uk")
        exc = caught.exception
        self.assertEqual(exc.code, "insufficient_tokens")
        self.assertEqual((exc.cost, exc.remaining, exc.resets_at), (0, 0, "2026-10-01T00:00:00Z"))
        self.assertEqual(exc.request_id, "or_req_00000000000000000000000000000000")
        self.assertEqual(exc.tokens.cost, 0)

    def test_errors_are_typed_and_keep_code_and_request_id(self):
        cases = [
            (400, "error-400.unknown_sport.json", BadRequestError, "unknown_sport"),
            (401, "error-401.invalid_api_key.json", AuthenticationError, "invalid_api_key"),
            (403, "error-403.sport_not_in_plan.json", PermissionDeniedError, "sport_not_in_plan"),
            (404, "error-404.unknown_event.json", NotFoundError, "unknown_event"),
            (503, "error-503.temporarily_unavailable.json", ServerError, "temporarily_unavailable"),
        ]
        for status, name, cls, code in cases:
            with self.subTest(status=status):
                c, _ = client(reply(status, fixture(name)))
                with self.assertRaises(cls) as caught:
                    c.event("or_evt_000000000000")
                exc = caught.exception
                self.assertIsInstance(exc, APIError)
                self.assertEqual((exc.status, exc.code), (status, code))
                self.assertTrue(exc.request_id.startswith("or_req_"))
                self.assertIn(code, str(exc))

    def test_empty_502_is_a_server_error_without_code(self):
        c, _ = client(reply(502, gzipped=False))
        with self.assertRaises(ServerError) as caught:
            c.status()
        self.assertIsNone(caught.exception.code)
        self.assertEqual(str(caught.exception), "HTTP 502")


class ResultTests(unittest.TestCase):
    def test_token_and_rate_limit_headers_on_every_result(self):
        headers = {
            "X-Tokens-Cost": "3",
            "X-Tokens-Used": "40",
            "X-Tokens-Remaining": "unlimited",
            "X-Tokens-Limit": "unlimited",
            "X-Tokens-Reset": "2026-11-01T00:00:00Z",
            "X-RateLimit-Limit": "20",
            "X-RateLimit-Remaining": "19",
            "X-RateLimit-Reset": "7",
            "X-OddsRelay-Request-Id": "or_req_2",
        }
        c, _ = client(reply(body=fixture("v2-odds-raw.page.json"), headers=headers))
        result = c.raw(region="uk")
        self.assertEqual(tuple(result.tokens), (3, 40, "unlimited", "unlimited", "2026-11-01T00:00:00Z"))
        self.assertEqual(tuple(result.rate_limit), (20, 19, 7))
        self.assertEqual(result.request_id, "or_req_2")

    def test_missing_headers_read_as_none(self):
        c, _ = client(reply(body=fixture("v2-status.status.json")))
        result = c.status()
        self.assertEqual(tuple(result.tokens), (None,) * 5)
        self.assertEqual(tuple(result.rate_limit), (None,) * 3)

    def test_unknown_fields_are_kept(self):
        page = json.loads(fixture("v2-odds-raw.page.json"))
        page["field_added_later"] = {"x": 1}
        page["data"][0]["another_new_field"] = [1, 2]
        c, _ = client(reply(body=json.dumps(page).encode()))
        self.assertEqual(c.raw().data, page)
        unknown = json.loads(fixture("error-400.unknown_sport.json"))
        c, _ = client(reply(400, json.dumps(unknown).encode()))
        with self.assertRaises(BadRequestError) as caught:
            c.odds("standard", sports="curling_xyz")
        self.assertEqual(caught.exception.error["valid"], unknown["error"]["valid"])


class RouteTests(unittest.TestCase):
    def test_one_method_per_get_route(self):
        base = oddsrelay.BASE_URL
        calls = [
            (lambda c: c.odds("each-way"), "/v2/odds/each-way"),
            (lambda c: c.raw(), "/v2/odds/raw"),
            (lambda c: c.event("or_evt_1"), "/v2/odds/event/or_evt_1"),
            (lambda c: c.sports(), "/v2/sports"),
            (lambda c: c.events("soccer"), "/v2/events?sport=soccer"),
            (lambda c: c.bookmakers(), "/v2/bookmakers"),
            (lambda c: c.regions(), "/v2/regions"),
            (lambda c: c.coverage(), "/v2/coverage"),
            (lambda c: c.status(), "/v2/status"),
            (lambda c: c.health(), "/v2/health"),
            (lambda c: c.usage(), "/v2/usage"),
            (lambda c: c.pricing(), "/v2/pricing"),
            (lambda c: c.openapi(), "/v2/openapi.json"),
            (lambda c: c.healthz(), "/healthz"),
        ]
        for call, path in calls:
            with self.subTest(path=path):
                c, opener = client(reply(body=b"{}"))
                call(c)
                self.assertEqual(opener.requests[0].full_url, base + path)
                self.assertEqual(opener.requests[0].get_method(), "GET")

    def test_healthz_is_text(self):
        c, _ = client(reply(body=fixture("healthz.txt")))
        self.assertEqual(c.healthz().data.splitlines()[0], "ok")

    def test_keyless_routes_read_fixtures(self):
        c, _ = client(reply(body=fixture("v2-status.status.json")), reply(body=fixture("v2-coverage.keyless.json")))
        self.assertEqual(c.status(region="uk").data["region"], "uk")
        self.assertIn("products", c.coverage(region="uk").data)

    def test_filters_are_keyword_arguments(self):
        c, opener = client(reply(body=b"{}"))
        start = datetime(2026, 10, 9, 13, 30, 15, tzinfo=timezone(timedelta(hours=1)))
        c.odds(
            "standard",
            region="uk",
            sports=["soccer", "tennis"],
            odds_format="decimal",
            commence_time_from=start,
            commenceTimeTo="2026-10-10T00:00:00Z",
            exchanges=None,
        )
        self.assertEqual(
            query(opener.requests[0]),
            {
                "region": "uk",
                "sports": "soccer,tennis",
                "oddsFormat": "decimal",
                "commenceTimeFrom": "2026-10-09T12:30:15Z",
                "commenceTimeTo": "2026-10-10T00:00:00Z",
            },
        )

    def test_path_values_are_escaped(self):
        c, opener = client(reply(body=b"{}"))
        c.event("a/b?c")
        self.assertTrue(opener.requests[0].full_url.endswith("/v2/odds/event/a%2Fb%3Fc"))


class SourceTests(unittest.TestCase):
    SOURCES = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))

    def test_runtime_imports_only_the_standard_library(self):
        allowed = {"__future__", "datetime", "email", "gzip", "json", "os", "re", "time", "typing", "urllib"}
        for path in (ROOT / "src").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")

    def test_sources_parse_as_python_3_9(self):
        for path in self.SOURCES:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))

    def test_version_matches_pyproject(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1), oddsrelay.__version__)
        self.assertRegex(oddsrelay.CONTRACT_VERSION, r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
