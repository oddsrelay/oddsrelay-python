"""The OddsRelay Python client: one method per GET route of the OddsRelay API, standard library only."""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import Message
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple, Union

__version__ = "0.1.0"
CONTRACT_VERSION = "2026-07-08"  # info.version of the API description these methods follow
BASE_URL = "https://api.oddsrelay.io"

__all__ = [
    "APIError",
    "AuthenticationError",
    "BASE_URL",
    "BadRequestError",
    "CONTRACT_VERSION",
    "InsufficientTokensError",
    "NotFoundError",
    "NotModified",
    "OddsRelay",
    "OddsRelayError",
    "PermissionDeniedError",
    "QuoteTooHigh",
    "RateLimit",
    "RateLimitError",
    "Result",
    "ServerError",
    "Tokens",
]

_TRIES = 3  # a 429 is retried until this many requests have been made
_CACHE_SIZE = 32  # URLs whose last ETag and body are kept for conditional requests
_USER_AGENT = f"oddsrelay-python/{__version__}"


class Tokens(NamedTuple):
    """The X-Tokens-* headers. None when a header is absent; remaining and limit may be "unlimited"."""

    cost: Optional[int]
    used: Optional[int]
    remaining: Union[int, str, None]
    limit: Union[int, str, None]
    reset: Optional[str]


class RateLimit(NamedTuple):
    """The X-RateLimit-* headers, for whichever rate window is nearest its limit. None when absent."""

    limit: Optional[int]
    remaining: Optional[int]
    reset: Optional[int]


def _int(value: Optional[str]) -> Any:
    return int(value) if value is not None and value.isdigit() else value


class _WithHeaders:
    headers: Message

    @property
    def tokens(self) -> Tokens:
        h = self.headers
        return Tokens(
            _int(h.get("X-Tokens-Cost")),
            _int(h.get("X-Tokens-Used")),
            _int(h.get("X-Tokens-Remaining")),
            _int(h.get("X-Tokens-Limit")),
            h.get("X-Tokens-Reset"),
        )

    @property
    def rate_limit(self) -> RateLimit:
        h = self.headers
        return RateLimit(
            _int(h.get("X-RateLimit-Limit")),
            _int(h.get("X-RateLimit-Remaining")),
            _int(h.get("X-RateLimit-Reset")),
        )


class Result(_WithHeaders):
    """A successful reply. `data` is the decoded body with every field kept (text for `healthz()`)."""

    not_modified = False

    def __init__(self, status: int, data: Any, headers: Message, url: str) -> None:
        self.status = status
        self.data = data
        self.headers = headers
        self.url = url

    @property
    def etag(self) -> Optional[str]:
        return self.headers.get("ETag")

    @property
    def request_id(self) -> Optional[str]:
        return self.headers.get("X-OddsRelay-Request-Id")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, url={self.url!r})"


class NotModified(Result):
    """A 304: nothing changed since the last reply for this URL, and no tokens were spent.

    `data` is the body of that last reply, kept by the client.
    """

    not_modified = True


class OddsRelayError(Exception):
    """Base class for the errors this package raises itself. Network failures stay the standard library's OSError."""


class APIError(OddsRelayError, _WithHeaders):
    """An error reply. Branch on `code` (None on the empty-bodied 429 and 502), never on `message`."""

    def __init__(self, status: int, error: Dict[str, Any], headers: Message) -> None:
        self.status = status
        self.error = error  # the body's "error" object, every field kept
        self.code: Optional[str] = error.get("code")
        self.message: str = error.get("message") or f"HTTP {status}"
        self.headers = headers
        self.request_id: Optional[str] = error.get("request_id") or headers.get("X-OddsRelay-Request-Id")
        text = f"{status} {self.code}: {self.message}" if self.code else self.message
        super().__init__(text + (f" (request_id {self.request_id})" if self.request_id else ""))


class BadRequestError(APIError):
    """400: a parameter was refused. `error.get("valid")` lists accepted values where the API sends them."""


class AuthenticationError(APIError):
    """401: the key is missing, unknown, revoked, expired or paused."""


class InsufficientTokensError(APIError):
    """402: the call costs more tokens than the account has left. No data was returned and no tokens were taken."""

    def __init__(self, status: int, error: Dict[str, Any], headers: Message) -> None:
        super().__init__(status, error, headers)
        self.cost: Optional[int] = error.get("cost")
        self.remaining: Optional[int] = error.get("remaining")
        self.resets_at: Optional[str] = error.get("resets_at")


class PermissionDeniedError(APIError):
    """403: outside the account's plan, scope or region."""


class NotFoundError(APIError):
    """404: an unknown route, board type, event or region."""


class RateLimitError(APIError):
    """429, still there after the client's retries. `retry_after` is in seconds."""

    def __init__(self, status: int, error: Dict[str, Any], headers: Message) -> None:
        super().__init__(status, error, headers)
        self.retry_after: int = _seconds(headers.get("Retry-After"))


class ServerError(APIError):
    """5xx: try again later."""


class QuoteTooHigh(OddsRelayError):
    """The quote for a call was above the `max_tokens` you passed, so the call was not made. Nothing was spent."""

    def __init__(self, cost: int, max_tokens: int, quote: Result) -> None:
        self.cost = cost
        self.max_tokens = max_tokens
        self.quote = quote
        super().__init__(f"this call costs {cost} tokens, above max_tokens={max_tokens}: add filters to narrow it")


_ERRORS = {
    400: BadRequestError,
    401: AuthenticationError,
    402: InsufficientTokensError,
    403: PermissionDeniedError,
    404: NotFoundError,
    429: RateLimitError,
}


def _seconds(value: Optional[str]) -> int:
    return int(value) if value and value.isdigit() else 1


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(map(str, value))
    if isinstance(value, datetime):  # naive datetimes are read as UTC, as the API reads them
        moment: datetime = value.astimezone(timezone.utc) if value.tzinfo else value
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _camel(name: str) -> str:
    # odds_format and oddsFormat both work: snake_case names are sent in the API's camelCase.
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)


def _query(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode([(_camel(k), _param(v)) for k, v in params.items() if v is not None])


def _segment(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


class OddsRelay:
    """A client for the OddsRelay API.

    key: your API key. Left out, it is read from the ODDSRELAY_KEY environment variable. The keyless
    routes (status, coverage, openapi, healthz) work without one.
    """

    def __init__(
        self,
        key: Optional[str] = None,
        *,
        timeout: float = 30.0,
        opener: Optional[urllib.request.OpenerDirector] = None,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        # Stripped, since a key read from a file or a .env often ends in a newline. The check stops http.client
        # from raising an error whose message would carry the whole key; this message never echoes it.
        key = (key or os.environ.get("ODDSRELAY_KEY") or "").strip()
        if key and not re.fullmatch(r"[!-~]+", key):
            raise OddsRelayError("the API key holds characters an HTTP header cannot carry: copy it again")
        self._key = key or None
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep
        self._cache: Dict[str, Tuple[str, Any]] = {}

    def __repr__(self) -> str:
        return f"OddsRelay(key={'set' if self._key else 'not set'})"

    # The matched boards and raw odds. These cost tokens: quote=True prices a call without making it,
    # and max_tokens=N quotes first and raises QuoteTooHigh instead of making a call that costs more.

    def odds(self, type: str, *, max_tokens: Optional[int] = None, **params: Any) -> Result:
        """GET /v2/odds/{type}: matched odds for one board: standard, 2up, dutching, each-way, extra-place or bog."""
        return self._priced(f"/v2/odds/{_segment(type)}", params, max_tokens)

    def raw(self, *, max_tokens: Optional[int] = None, **params: Any) -> Result:
        """GET /v2/odds/raw: bookmaker and exchange prices by event, unpaired; page through with cursor and limit."""
        return self._priced("/v2/odds/raw", params, max_tokens)

    def event(self, id: str, *, max_tokens: Optional[int] = None, **params: Any) -> Result:
        """GET /v2/odds/event/{id}: one event's raw prices."""
        return self._priced(f"/v2/odds/event/{_segment(id)}", params, max_tokens)

    # Discovery and account routes.

    def sports(self, **params: Any) -> Result:
        """GET /v2/sports: the sport and competition keys."""
        return self._get("/v2/sports", params)

    def events(self, sport: str, **params: Any) -> Result:
        """GET /v2/events: upcoming events for one sport key."""
        return self._get("/v2/events", {"sport": sport, **params})

    def bookmakers(self, **params: Any) -> Result:
        """GET /v2/bookmakers: the venues you can filter on, with their keys."""
        return self._get("/v2/bookmakers", params)

    def regions(self) -> Result:
        """GET /v2/regions: the region codes open to your key."""
        return self._get("/v2/regions")

    def coverage(self, **params: Any) -> Result:
        """GET /v2/coverage: which venues and sports each product carries. Keyless; more detail with a key."""
        return self._get("/v2/coverage", params)

    def status(self, **params: Any) -> Result:
        """GET /v2/status: whether each product is serving. Keyless."""
        return self._get("/v2/status", params)

    def health(self) -> Result:
        """GET /v2/health: when each board was last processed, and whether any is behind."""
        return self._get("/v2/health")

    def usage(self) -> Result:
        """GET /v2/usage: tokens used and left, and what your key may read."""
        return self._get("/v2/usage")

    def pricing(self) -> Result:
        """GET /v2/pricing: the token rate card."""
        return self._get("/v2/pricing")

    def openapi(self) -> Result:
        """GET /v2/openapi.json: the API description. Keyless."""
        return self._get("/v2/openapi.json")

    def healthz(self) -> Result:
        """GET /healthz: a plain-text liveness check. Keyless."""
        return self._get("/healthz")

    def _priced(self, path: str, params: Dict[str, Any], max_tokens: Optional[int]) -> Result:
        if max_tokens is not None:
            quote = self._get(path, {**params, "quote": True})
            cost = quote.data["cost"]
            if cost > max_tokens:
                raise QuoteTooHigh(cost, max_tokens, quote)
        return self._get(path, params)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Result:
        query = _query(params or {})
        url = BASE_URL + path + ("?" + query if query else "")
        cached = self._cache.get(url)
        for attempt in range(1, _TRIES + 1):
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": _USER_AGENT})
            if self._key:  # unredirected: urllib never copies it onto a redirect
                req.add_unredirected_header("Authorization", f"Bearer {self._key}")
            if cached:
                req.add_header("If-None-Match", cached[0])
            try:
                with self._opener.open(req, timeout=self.timeout) as res:
                    status, headers, body = res.status, res.headers, res.read()
            except urllib.error.HTTPError as err:  # 304 and every error status land here
                status, headers, body = err.code, err.headers, err.read()
            if body and headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if status != 429 or attempt == _TRIES:
                break
            self._sleep(_seconds(headers.get("Retry-After")))
        if status == 304:
            return NotModified(status, cached[1] if cached else None, headers, url)
        if status >= 400:
            raise self._error(status, body, headers)
        if headers.get_content_type().endswith("json"):
            data = json.loads(body)
        else:
            data = body.decode("utf-8")
        etag = headers.get("ETag")
        if etag:
            self._cache.pop(url, None)
            self._cache[url] = (etag, data)
            if len(self._cache) > _CACHE_SIZE:  # ponytail: drops the oldest stored URL, not an LRU
                del self._cache[next(iter(self._cache))]
        return Result(status, data, headers, url)

    def _error(self, status: int, body: bytes, headers: Message) -> APIError:
        try:
            error = json.loads(body)["error"]
            if not isinstance(error, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            error = {}  # the front door's 429 and 502 have an empty body
        if self._key:  # a key never reaches an exception, even if a message ever echoed one
            error = {k: v.replace(self._key, "***") if isinstance(v, str) else v for k, v in error.items()}
        cls = _ERRORS.get(status, ServerError if status >= 500 else APIError)
        return cls(status, error, headers)
