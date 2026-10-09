# Changelog

## 0.1.0 (2026-10-09)

The first version.

- One method for each GET route of the OddsRelay API, following API description version 2026-07-08.
- The key comes from `ODDSRELAY_KEY` or an argument and travels only in the `Authorization` header.
- Every request asks for gzip and the reply is decompressed.
- `quote=True` prices a call; `max_tokens=` quotes first and raises `QuoteTooHigh` above your cap.
- Conditional requests: each URL's `ETag` is sent back as `If-None-Match`, and a 304 returns `NotModified`.
- A 429 waits `Retry-After` and is retried, three requests in all.
- Errors are typed by status and carry `code` and `request_id`; a 402 carries `cost`, `remaining` and `resets_at`.
- Token and rate-limit headers on every result and error.
