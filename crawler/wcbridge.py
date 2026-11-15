"""(15 Nov) The crawler's REST calls to the shop through the SSH bridge instead of HTTPS.

WC_BRIDGE=host:port (and the token in WC_BRIDGE_TOKEN_FILE, default /data/tmp/bridge.token) makes
WC._wc / WC._wp send every call through tools/bridge_proxy.py on the crawler host to a WordPress process
on the shop (tools/shop_bridge.php). The answers look like `requests` responses, so all of publish.py,
catmake.py and the brands code work unchanged: same payloads, same retries, same checks.
Why: the shop's MariaDB was killed for want of memory five times on 14 Nov while products went in over
HTTPS (every call took a php-fpm worker of the site's visitors).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import threading

import requests

RESTART_AFTER = 300                                     # a fresh WordPress process now and then
_local = threading.local()


def enabled() -> bool:
    return bool(os.environ.get("WC_BRIDGE", "").strip())


class Response:
    """The parts of requests.Response the WC client reads."""

    def __init__(self, status: int, body, headers: dict | None = None):
        self.status_code = int(status)
        self._body = body
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return self._body


class _Conn:
    def __init__(self, addr: str, token: str, timeout: int):
        host, port = addr.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        self.sock.settimeout(timeout)
        self.f = self.sock.makefile("rb")
        self.sock.sendall(token.encode() + b"\n")
        first = self._answer()
        if not first.get("ready"):
            raise requests.ConnectionError(f"bridge not ready: {first}")
        self.used = 0

    def _answer(self) -> dict:
        while True:
            line = self.f.readline()
            if not line:
                raise requests.ConnectionError("bridge closed (the WordPress process on the shop ended)")
            if line.startswith(b"@@E "):
                raise requests.ConnectionError("bridge refused: " + line[4:].decode(errors="ignore").strip())
            if line.startswith(b"@@R "):
                return json.loads(line[4:])
            # anything else is a plugin's output: ignored

    def call(self, q: dict) -> dict:
        self.sock.sendall(json.dumps(q, ensure_ascii=False).encode() + b"\n")
        self.used += 1
        return self._answer()

    def close(self) -> None:
        try:
            self.sock.sendall(b"QUIT\n")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _conn(timeout: int) -> _Conn:
    c = getattr(_local, "c", None)
    if c is not None and c.used >= RESTART_AFTER:
        c.close()
        c = None
    if c is None:
        tf = os.environ.get("WC_BRIDGE_TOKEN_FILE", "/data/tmp/bridge.token")
        with open(tf) as fh:
            token = fh.read().strip()
        c = _Conn(os.environ["WC_BRIDGE"].strip(), token, timeout)
        _local.c = c
    return c


def request(api: str, method: str, path: str, params=None, json_body=None, data: bytes | None = None,
            headers: dict | None = None, timeout: int = 300) -> Response:
    """One REST call on the shop. Network trouble raises requests.ConnectionError (WC._call retries it)."""
    q = {"api": api, "method": method.upper(), "path": path.lstrip("/")}
    if params:
        q["params"] = {k: v for k, v in params.items() if v is not None}
    if json_body is not None:
        q["json"] = json_body
    if data is not None:
        q["body_b64"] = base64.b64encode(data).decode()
    if headers:
        q["headers"] = headers
    c = _conn(max(int(timeout or 0), 120))
    try:
        a = c.call(q)
    except (OSError, ValueError, requests.ConnectionError) as e:
        c.close()
        _local.c = None
        raise requests.ConnectionError(f"bridge: {e}") from e
    return Response(a.get("status", 500), a.get("body"), a.get("headers"))


def close() -> None:
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None
