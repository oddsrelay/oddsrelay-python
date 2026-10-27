# oddsrelay

The OddsRelay Python client. One method for each live-feed data and lookup route of the [OddsRelay API](https://oddsrelay.io/docs),
which serves bookmaker and exchange odds for the UK & Ireland as JSON. Standard library only, Python 3.9 and up.

## Install

```sh
pip install oddsrelay
```

## Get a key

Sign up at [app.oddsrelay.io/signup](https://app.oddsrelay.io/signup) and copy your key from the dashboard.
Free gives you 50,000 tokens for 14 days from sign-up, with no card, then no tokens until you choose a plan, on the same key. [Plans and token prices](https://oddsrelay.io/pricing).
Paid plans are for your own use. A product that serves other people runs on Enterprise.

## First call

Put your key in your environment, never in your code:

```sh
export ODDSRELAY_KEY="your key"
```

```python
from oddsrelay import OddsRelay

client = OddsRelay()  # reads ODDSRELAY_KEY

result = client.odds("standard", region="uk", sports="soccer", markets="h2h")  # soccer is the API's key for football
for event in result.data["data"]:
    teams = f"{event['home_team']} v {event['away_team']}" if event.get("home_team") else event.get("event_name")
    print(event["commence_time"], teams)

print(result.tokens)  # what the call cost and what is left
```

No key yet? These routes answer without one:

```python
client = OddsRelay()
print(client.status().data)
print(client.coverage(region="uk").data["products"].keys())
```

## Methods

| Method | Route | What it returns |
|---|---|---|
| `odds(type, **filters)` | `GET /v2/odds/{type}` | Matched odds for one board type: `standard`, `2up`, `dutching`, `each-way`, `extra-place` or `bog` |
| `raw(**filters)` | `GET /v2/odds/raw` | Bookmaker and exchange prices by event, unpaired, a page at a time |
| `event(id, **filters)` | `GET /v2/odds/event/{id}` | One event's raw prices |
| `sports(**filters)` | `GET /v2/sports` | Sport and competition keys |
| `events(sport, **filters)` | `GET /v2/events` | Upcoming events for one sport key |
| `bookmakers(**filters)` | `GET /v2/bookmakers` | The venues you can filter on, with their keys |
| `regions()` | `GET /v2/regions` | The region codes open to your key |
| `coverage(**filters)` | `GET /v2/coverage` | Which venues and sports each product carries (keyless; more detail with a key) |
| `status(**filters)` | `GET /v2/status` | Whether each product is serving (keyless) |
| `health()` | `GET /v2/health` | When each board was last processed, and whether any is behind |
| `usage()` | `GET /v2/usage` | Tokens used and left, and what your key may read |
| `pricing()` | `GET /v2/pricing` | The token rate card |
| `openapi()` | `GET /v2/openapi.json` | The API description (keyless) |
| `healthz()` | `GET /healthz` | A plain-text liveness check (keyless) |

The historical odds routes (`GET /v2/historical/*`) have no method yet.

Each method returns a `Result`. `result.data` is the decoded body, with every field kept, including fields the API
adds after this release.

## Filters

Every query parameter in the [API reference](https://oddsrelay.io/docs/reference) is a keyword argument. Python's
snake_case and the API's camelCase both work, so `odds_format="decimal"` and `oddsFormat="decimal"` send the same
thing. Lists become comma-separated values, `True` and `False` become `true` and `false`, a `datetime` becomes a UTC
timestamp, and `None` is left out:

```python
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
result = client.odds(
    "each-way",
    region="uk",
    sports=["horse_racing"],
    commence_time_from=now,
    commence_time_to=now + timedelta(hours=6),
)
```

The API checks every filter itself: an unknown name or value raises `BadRequestError`, and where the API lists the
accepted values, `error.get("valid")` holds them.

## Price a call before you make it

`quote=True` asks what a call would cost instead of making it. A quote spends no tokens:

```python
quote = client.odds("standard", region="uk", quote=True)
print(quote.data["cost"], quote.data["remaining"])
```

Or let the client do it. With `max_tokens`, `odds()`, `raw()` and `event()` quote first and raise `QuoteTooHigh`,
without making the call, when the cost is above your cap:

```python
from oddsrelay import QuoteTooHigh

try:
    result = client.raw(region="uk", sports="tennis", max_tokens=cap)  # cap: the most you will spend on this call
except QuoteTooHigh as exc:
    print(f"this call costs {exc.cost} tokens: add filters to narrow it")
```

## Polling: spend tokens only on changes

The client keeps the `ETag` of each URL it has fetched and sends it back as `If-None-Match`. When nothing has changed
the API answers 304, which spends no tokens, and you get a `NotModified` result whose `data` is the body you already
have:

```python
import time

while True:
    result = client.odds("standard", region="uk", sports="soccer")
    if not result.not_modified:
        update_my_screen(result.data)
    time.sleep(poll_seconds)
```

`NotModified` is a subclass of `Result`, so code that reads `result.data` works either way.

## Paging

`raw()`, `sports()` and `events()` return a page at a time. Pass the next page's cursor back as `cursor`:

```python
cursor = None
while True:
    page = client.raw(region="uk", sports="tennis", cursor=cursor)
    handle(page.data["data"])
    cursor = page.data["meta"].get("next_cursor")
    if not cursor:
        break
```

## Errors

A reply the API refuses raises an `APIError` subclass with `status`, `code`, `message`, `request_id` and `error` (the
whole error object). `APIError` and `QuoteTooHigh` are both `OddsRelayError`s. Network failures are not wrapped: a
refused connection or a timeout raises the standard library's `OSError` (such as `urllib.error.URLError`), so a
long-running loop catches `(OddsRelayError, OSError)`.

| Status | Exception |
|---|---|
| 400 | `BadRequestError` |
| 401 | `AuthenticationError` |
| 402 | `InsufficientTokensError`, with `cost`, `remaining` and `resets_at` |
| 403 | `PermissionDeniedError` |
| 404 | `NotFoundError` |
| 429 | `RateLimitError`, with `retry_after`, once the client's own retries are used up |
| 5xx | `ServerError` |

Branch on `exc.code`, never on the message, which may change. Quote `request_id` when you contact us.

```python
from oddsrelay import InsufficientTokensError

try:
    result = client.odds("dutching", region="uk")
except InsufficientTokensError as exc:
    print(f"needs {exc.cost} tokens, {exc.remaining} left until {exc.resets_at}")
```

A 429 is retried for you: the client waits the `Retry-After` seconds and tries again, three requests in all. Pass
your own `sleep=` to `OddsRelay()` to change how it waits.

## Tokens and rate limits

Every `Result`, and every `APIError`, carries the reply's headers:

- `tokens`: `cost`, `used`, `remaining`, `limit` and `reset`, from the `X-Tokens-*` headers
- `rate_limit`: `limit`, `remaining` and `reset`, from the `X-RateLimit-*` headers
- `headers`, `etag` and `request_id`

A header the reply did not carry reads as `None`.

## Your key

The key travels only in the `Authorization` header. The client never puts it in a URL, a log line, a `repr` or an
exception message, and it is not forwarded if a reply ever redirects. Spaces and newlines around the key, common when
it is read from a file, are dropped.

## Versions

`oddsrelay.CONTRACT_VERSION` names the version of the API description these methods follow. The API only adds to
`/v2`, so new fields and new error codes reach you without an upgrade.

## More from OddsRelay

- [oddsrelay-js](https://github.com/oddsrelay/oddsrelay-js): the JavaScript and TypeScript client, `npm install oddsrelay`
- [oddsrelay-r](https://github.com/oddsrelay/oddsrelay-r): the R client
- [oddsrelay-mcp](https://github.com/oddsrelay/oddsrelay-mcp): an MCP server that gives Claude, Cursor and other AI assistants read access, with a token cap
- [oddsrelay-sheets](https://github.com/oddsrelay/oddsrelay-sheets): formulas and a template spreadsheet for Google Sheets
- [examples](https://github.com/oddsrelay/examples): short programs to copy, one folder each
- [Libraries and tools](https://oddsrelay.io/docs/libraries), the [docs](https://oddsrelay.io/docs), the [API reference](https://oddsrelay.io/docs/reference) and the [OpenAPI file](https://api.oddsrelay.io/v2/openapi.json)
- Contact: hello@oddsrelay.io

## Licence

MIT. See [LICENSE](https://github.com/oddsrelay/oddsrelay-python/blob/main/LICENSE).
