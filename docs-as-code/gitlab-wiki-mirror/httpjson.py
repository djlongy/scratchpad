"""Minimal JSON-over-HTTPS client shared by the runtime helpers.

The predecessor templates parsed API responses with `grep -o '"key":"[^"]*"'`,
which returns an empty string for a field the server never sent. An absent
`readiness` field then compared unequal to `"red"` and the release gate passed on
an empty response (audit 6.2). Everything here uses a real parser and treats a
response it cannot parse as a failure.

Only the standard library: runtime/<domain>/install.sh writes these helpers
into a job's runtime directory, and nothing is installed there.

Nothing in this module logs a header value or a request body. Credentials reach
it as arguments and stay there.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    """The request could not be completed, which is never a passing result."""


class Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parsed body, or HttpError.

        A body that is not JSON is a failure and not an empty result: the caller
        asked an API a question and did not get an answer it can read.
        """
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as error:
            raise HttpError(f"response body is not JSON: {error}") from error

    def ok(self) -> bool:
        return 200 <= self.status < 300


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
) -> Response:
    """Perform one HTTPS request and return its status and body.

    An HTTP error status is returned, not raised: callers decide which codes are
    expected. Only a transport-level failure raises, because that is a question
    that never reached the server.
    """
    if not url.startswith("https://"):
        # TLS is not optional, and neither is a maintained trust bundle
        # (section 10.2). A plaintext endpoint is a configuration error.
        raise HttpError(f"refusing a non-HTTPS request to {url}")

    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return Response(response.status, response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read())
    except urllib.error.URLError as error:
        raise HttpError(f"{method} {url} could not be completed: {error.reason}") from error
    except TimeoutError as error:
        raise HttpError(f"{method} {url} timed out after {timeout}s") from error


def get_json(url: str, **kwargs: Any) -> Any:
    response = request("GET", url, **kwargs)
    if not response.ok():
        raise HttpError(f"GET {url} returned HTTP {response.status}: {response.text[:300]}")
    return response.json()
