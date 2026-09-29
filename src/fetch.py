"""HTTP requests with a timeout and at most two retries. No client framework."""

import json
import math
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

__all__ = ["SourceError", "fetch_text", "fetch_json", "number", "items"]


class SourceError(RuntimeError):
    """A public source is unavailable or returned an unexpected response."""


def fetch_text(url, *, params=None, data=None, headers=None, timeout=20):
    # Build a GET, or a JSON POST when a request body is supplied.
    parsed_url = urlsplit(url)
    host = parsed_url.hostname
    if params:
        query = urlencode(params)
        url += "?" + query

    request_headers = {"User-Agent": "TrendEngine/0.2 (public trend research)"}
    body = None
    if data is not None:
        request_headers["Content-Type"] = "application/json"
        json_text = json.dumps(data)
        body = json_text.encode()

    request_headers.update(headers or {})
    request = Request(url, data=body, headers=request_headers)

    # Retry temporary failures only, with a short wait between attempts.
    for attempt in range(3):
        delay = 2**attempt
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
                return body.decode("utf-8")
        except HTTPError as exc:
            # Never include a credential-bearing URL or response body in reports.
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise SourceError("%s: HTTP %s" % (host, status)) from exc

            if retry_after:
                if not retry_after.isdigit():
                    raise SourceError(
                        "%s: HTTP %s; retry later" % (host, status)
                    ) from exc

                retry_seconds = int(retry_after)
                if retry_seconds > 60:
                    raise SourceError(
                        "%s: HTTP %s; retry later" % (host, status)
                    ) from exc

                delay = max(delay, retry_seconds)
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            if attempt == 2:
                raise SourceError("%s: connection failed or timed out" % host) from exc

        time.sleep(delay)

    raise AssertionError("unreachable")


def fetch_json(url, **kwargs):
    try:
        text = fetch_text(url, **kwargs)
        # JSON permits non-finite numbers in Python; reports use null instead.
        return json.loads(text, parse_float=number, parse_constant=_unknown_constant)
    except (ValueError, UnicodeError) as exc:
        parsed_url = urlsplit(url)
        raise SourceError("%s: invalid JSON" % parsed_url.hostname) from exc


def _unknown_constant(value):
    # NaN and positive/negative infinity represent unknown source metrics.
    return None


def number(value):
    """Missing, malformed, and non-finite source metrics remain unknown."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if not math.isfinite(result):
        return None

    return result


def items(payload, key):
    if not isinstance(payload, dict):
        raise SourceError("Response must contain a '%s' array" % key)

    rows = payload.get(key)
    if not isinstance(rows, list):
        raise SourceError("Response must contain a '%s' array" % key)

    # Validate each entry before a source parser starts reading its fields.
    for item in rows:
        if not isinstance(item, dict):
            raise SourceError("'%s' must contain objects" % key)

    return rows
