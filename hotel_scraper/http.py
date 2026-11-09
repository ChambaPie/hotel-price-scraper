"""Tiny HTTP client on the standard library: cookie session, gzip, timing, and no exceptions on HTTP errors.
Every request returns a Response; status 0 means the request never got an answer (DNS, timeout, reset)."""
from __future__ import annotations

import gzip
import json
import time
import zlib
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed: float = 0.0
    url: str = ""
    error: str = ""

    def header(self, name: str, default: str = "") -> str:
        name = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == name), default)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


def _decode(body: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
    except (OSError, zlib.error):
        pass
    return body


class Session:
    def __init__(self, user_agent: str, timeout: float = 30, headers: dict | None = None):
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.timeout = timeout
        self.base_headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", **(headers or {})}

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> Response:
        req = Request(url, data=data, headers={**self.base_headers, **(headers or {})}, method=method)
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                body = _decode(r.read(), r.headers.get("Content-Encoding"))
                return Response(r.status, dict(r.headers), body, time.monotonic() - t0, r.geturl())
        except HTTPError as e:
            body = _decode(e.read() or b"", e.headers.get("Content-Encoding") if e.headers else None)
            return Response(e.code, dict(e.headers or {}), body, time.monotonic() - t0, url, f"HTTP {e.code}")
        except (URLError, TimeoutError, OSError) as e:
            return Response(0, {}, b"", time.monotonic() - t0, url, str(getattr(e, "reason", e)))

    def get(self, url: str, headers: dict | None = None) -> Response:
        return self.request("GET", url, headers=headers)

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        return self.request("POST", url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
