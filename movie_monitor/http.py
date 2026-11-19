from __future__ import annotations

import gzip
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    text: str

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise FetchError("Expected JSON, received HTML or malformed data", self.status) from exc


class HttpClient:
    _host_lock = threading.Lock()
    _host_requests: dict[str, float] = {}

    def __init__(self, timeout: int = 25, min_gap: float = 1.0):
        self.timeout = timeout
        self.min_gap = min_gap
        self.count = 0
        self.last_request: dict[str, float] = {}
        self.bms_session = None

    def _pace(self, url: str):
        host = urllib.parse.urlsplit(url).netloc
        with self._host_lock:
            scheduled = max(time.monotonic(), self._host_requests.get(host, 0) + self.min_gap)
            self._host_requests[host] = scheduled
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last_request[host] = time.monotonic()
        self.count += 1

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        browser_http: bool = False,
    ) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        self._pace(url)
        try:
            if browser_http:
                from curl_cffi import requests

                if self.bms_session is None:
                    self.bms_session = requests.Session(impersonate="safari")
                res = self.bms_session.get(
                    url,
                    timeout=self.timeout,
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache", **(headers or {})},
                )
                if len(res.content) > 10_000_000:
                    raise FetchError("Response exceeded 10 MB limit", res.status_code)
                age = res.headers.get("Age", "0")
                if age.isdigit() and int(age) > 180:
                    raise FetchError("Provider cache age exceeds freshness limit", res.status_code)
                result = Response(res.status_code, res.text)
            else:
                req = urllib.request.Request(
                    url, headers={"Accept-Encoding": "gzip, deflate", **(headers or {})}
                )
                try:
                    res = urllib.request.urlopen(req, timeout=self.timeout)
                except urllib.error.HTTPError as exc:
                    res = exc
                with res:
                    age = res.headers.get("Age", "0")
                    if age.isdigit() and int(age) > 180:
                        raise FetchError("Provider cache age exceeds freshness limit", res.code)
                    body = res.read(10_000_001)
                    if len(body) > 10_000_000:
                        raise FetchError("Response exceeded 10 MB limit", res.code)
                    encoding = res.headers.get("Content-Encoding", "")
                    if body[:2] == b"\x1f\x8b" or encoding == "gzip":
                        body = gzip.decompress(body)
                    elif encoding == "deflate":
                        body = zlib.decompress(body)
                    result = Response(res.code, body.decode("utf-8", "replace"))
        except FetchError:
            raise
        except Exception as exc:
            # Deliberately omit request URLs, cookies and request headers.
            raise FetchError(f"Network/client failure: {type(exc).__name__}") from exc
        if result.status not in (200, 204):
            raise FetchError(f"HTTP {result.status}", result.status)
        return result

    def close(self):
        if self.bms_session is not None:
            self.bms_session.close()


def guest_headers(provider: str) -> dict:
    if provider != "district":
        raise ValueError("Guest headers are supported only for District")
    return {
        "api_source": provider,
        "x-app-type": "ed_mweb",
        "x-guest-token": f"{int(time.time() * 1000)}_{secrets.randbelow(10**18)}_{secrets.token_hex(6)}",
    }
