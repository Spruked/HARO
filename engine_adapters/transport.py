"""Cancellable HTTP transport shared by protocol adapters (stdlib only).

Cancellation is real on the client side: when the host's ``CancelToken`` fires,
a watcher thread shuts down the open socket, so a blocked read returns
immediately. Whether the *engine* also stops its work after the client
disconnects depends on that engine and must be verified per engine; adapters
report it as unverified until a live test shows it.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from .contract import AdapterError, CancelToken, CancelledByHost, EngineUnavailable


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(f"engine returned non-JSON body (HTTP {self.status})") from exc


class HttpError(AdapterError):
    def __init__(self, status: int, body: bytes):
        snippet = body[:500].decode("utf-8", errors="replace")
        super().__init__(f"HTTP {status}: {snippet}")
        self.status = status


def split_base_url(base_url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(base_url.rstrip("/"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported base URL: {base_url!r}")
    if parts.username or parts.password:
        raise ValueError("credentials must not be embedded in a base URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port, parts.path


class _Exchange:
    """One HTTP request whose socket can be closed from another thread."""

    def __init__(self, base_url: str, timeout_s: float, cancel: CancelToken | None):
        scheme, host, port, self.prefix = split_base_url(base_url)
        if scheme == "https":
            self.conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeout_s, context=ssl.create_default_context())
        else:
            self.conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        self.cancel = cancel
        # Whole-exchange deadline (separate from host cancellation).
        self.deadline: float | None = None
        self.timed_out = False
        self._done = threading.Event()
        self.aborted = False
        self._watcher: threading.Thread | None = None

    def start_watch(self) -> None:
        if self.cancel is None and self.deadline is None:
            return

        def abort() -> None:
            sock = getattr(self.conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def watch() -> None:
            while not self._done.wait(0.05):
                if self.cancel is not None and self.cancel.cancelled:
                    self.aborted = True
                    abort()
                    return
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.timed_out = True
                    abort()
                    return

        self._watcher = threading.Thread(target=watch, name="haro-http-cancel", daemon=True)
        self._watcher.start()

    def finish(self) -> None:
        self._done.set()
        try:
            self.conn.close()
        except OSError:
            pass
        if self._watcher is not None:
            self._watcher.join(timeout=1)


def _raise_for_cancel(ex: _Exchange, exc: BaseException) -> None:
    if ex.aborted or (ex.cancel is not None and ex.cancel.cancelled):
        raise CancelledByHost(ex.cancel.reason if ex.cancel else "cancelled") from exc


def request(method: str, base_url: str, path: str, *, json_body: Any = None,
            body: bytes | None = None, headers: Mapping[str, str] | None = None,
            timeout_s: float = 30.0, cancel: CancelToken | None = None,
            expected: tuple[int, ...] = (200,)) -> HttpResponse:
    """Blocking request that honours cancellation and the timeout."""
    if cancel is not None and cancel.cancelled:
        raise CancelledByHost(cancel.reason or "cancelled")
    hdrs = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    ex = _Exchange(base_url, timeout_s, cancel)
    ex.deadline = time.monotonic() + timeout_s
    try:
        ex.conn.request(method, ex.prefix + path, body=body, headers=hdrs)
        ex.start_watch()
        resp = ex.conn.getresponse()
        data = resp.read()
        result = HttpResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
    except (ConnectionRefusedError, socket.gaierror) as exc:
        raise EngineUnavailable(f"cannot connect to {base_url}: {type(exc).__name__}") from exc
    except (OSError, http.client.HTTPException) as exc:
        _raise_for_cancel(ex, exc)
        if ex.timed_out or isinstance(exc, (socket.timeout, TimeoutError)):
            raise TimeoutError(f"no complete response from {base_url}{path} within {timeout_s}s") from exc
        raise EngineUnavailable(f"transport failure to {base_url}: {type(exc).__name__}: {exc}") from exc
    finally:
        ex.finish()
    if cancel is not None and cancel.cancelled:
        raise CancelledByHost(cancel.reason or "cancelled")
    if result.status not in expected:
        raise HttpError(result.status, result.body)
    return result


def stream_lines(method: str, base_url: str, path: str, *, json_body: Any,
                 headers: Mapping[str, str] | None = None, timeout_s: float = 120.0,
                 cancel: CancelToken | None = None) -> Iterator[bytes]:
    """Yield raw response lines (NDJSON or SSE) until the stream ends.

    ``timeout_s`` bounds the whole exchange, not just one socket read.
    """
    if cancel is not None and cancel.cancelled:
        raise CancelledByHost(cancel.reason or "cancelled")
    hdrs = {"Content-Type": "application/json", **dict(headers or {})}
    ex = _Exchange(base_url, timeout_s, cancel)
    ex.deadline = time.monotonic() + timeout_s
    try:
        ex.conn.request(method, ex.prefix + path, body=json.dumps(json_body).encode("utf-8"), headers=hdrs)
        ex.start_watch()
        resp = ex.conn.getresponse()
        if resp.status != 200:
            raise HttpError(resp.status, resp.read())
        while True:
            line = resp.readline()
            if not line:
                break
            yield line
    except (ConnectionRefusedError, socket.gaierror) as exc:
        raise EngineUnavailable(f"cannot connect to {base_url}: {type(exc).__name__}") from exc
    except (OSError, http.client.HTTPException) as exc:
        _raise_for_cancel(ex, exc)
        if ex.timed_out or isinstance(exc, (socket.timeout, TimeoutError)):
            raise TimeoutError(f"stream from {base_url}{path} exceeded {timeout_s}s") from exc
        raise EngineUnavailable(f"stream failure from {base_url}: {type(exc).__name__}: {exc}") from exc
    finally:
        ex.finish()
    if cancel is not None and cancel.cancelled:
        raise CancelledByHost(cancel.reason or "cancelled")
    if ex.timed_out:
        raise TimeoutError(f"stream from {base_url}{path} exceeded {timeout_s}s")


def multipart(fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode multipart/form-data. files: name -> (filename, content, content_type)."""
    boundary = "haro-" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")
    for name, (filename, content, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"
