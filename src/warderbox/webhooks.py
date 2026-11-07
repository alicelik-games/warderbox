"""Webhook and callback notification system for WarderBox events.

Supports HTTP webhooks and in-process callbacks for events like:
- action_blocked: an action was blocked by rules or budget
- budget_warning: spending reached the warning threshold
- budget_exceeded: an action was blocked due to budget
- rate_limited: an action was blocked due to rate limits
- action_error: an action raised an exception
- action_completed: an action completed successfully

Usage:
    notifier = EventNotifier()
    notifier.on("action_blocked", my_callback)
    notifier.add_webhook("https://hooks.slack.com/...", events={"action_blocked"})

    notifier.emit("action_blocked", {"tool": "run_sql", "reason": "DROP TABLE"})
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

EventCallback = Callable[[str, dict[str, Any]], None]


def _ip_is_private(value: str) -> bool:
    """True for anything that is not a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True
    # Addresses that embed an IPv4 target are judged by that target.
    mapped = getattr(ip, "ipv4_mapped", None)
    embedded = getattr(ip, "sixtofour", None)
    if mapped is not None:
        ip = mapped
    elif embedded is not None:
        ip = embedded
    elif ip.version == 6 and ip in ipaddress.ip_network("64:ff9b::/96"):
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _is_private_host(host: str, resolve: bool = False) -> bool:
    """True for loopback/private/link-local literals and localhost.

    With ``resolve=True``, hostnames are resolved and every returned address
    must be public. Resolution failures fail closed. Registration is kept
    network-free; the send path resolves before connecting.
    """
    if not host:
        return True
    if host.lower().rstrip(".") in ("localhost", "localhost.localdomain"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _ip_is_private(host)
    if not resolve:
        return False  # hostname (resolved before the request is sent)
    return _resolve_public(host, 0) is None


def _resolve_public(host: str, port: int) -> list[tuple[int, str]] | None:
    """Resolve *host*; return its addresses only if every one is public.

    A failed lookup raises OSError: it is usually transient and worth a retry,
    unlike a private answer, which is refused for good.
    """
    infos = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    addresses: list[tuple[int, str]] = []
    for info in infos:
        address = info[4][0]
        if _ip_is_private(address):
            return None
        if (info[0], address) not in addresses:
            addresses.append((info[0], address))
    return addresses or None


def _connect_pinned(
    addresses: list[tuple[int, str]], port: int, timeout: float
) -> socket.socket:
    last_error: OSError | None = None
    for family, address in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((address, port))
            return sock
        except OSError as e:
            sock.close()
            last_error = e
    raise last_error or OSError("no addresses to connect to")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection to already validated addresses.

    The URL's hostname is kept for the Host header, but no further DNS lookup
    happens, so a DNS answer cannot change between the check and the connect.
    """

    def __init__(self, host, port, addresses, timeout, opener=None):
        super().__init__(host, port, timeout=timeout)
        self._pinned = addresses
        self._opener = opener

    def connect(self) -> None:
        self.sock = (
            self._opener() if self._opener
            else _connect_pinned(self._pinned, self.port, self.timeout)
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant: TLS SNI and certificate checks use the URL's hostname."""

    def __init__(self, host, port, addresses, timeout, context, opener=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned = addresses
        self._pinned_context = context
        self._opener = opener

    def connect(self) -> None:
        sock = (
            self._opener() if self._opener
            else _connect_pinned(self._pinned, self.port, self.timeout)
        )
        try:
            self.sock = self._pinned_context.wrap_socket(
                sock, server_hostname=self.host
            )
        except BaseException:
            sock.close()
            raise


class _RefusedDelivery(Exception):
    """Delivery must not be attempted or retried."""


class _ProxyRefused(OSError):
    """The proxy answered a CONNECT with something other than 200."""

    def __init__(self, status: int) -> None:
        super().__init__(f"proxy refused CONNECT with HTTP {status}")
        self.status = status


def _proxy_for(scheme: str, host: str) -> Any:
    """The configured proxy for this target, or None for a direct connection."""
    proxy = urllib.request.getproxies().get(scheme)
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return urlparse(proxy if "://" in proxy else f"http://{proxy}")


def _proxy_auth(proxy: Any) -> dict[str, str]:
    if not proxy.username:
        return {}
    pair = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
    token = base64.b64encode(pair.encode("utf-8")).decode("ascii")
    return {"Proxy-Authorization": f"Basic {token}"}


def _connect_proxy(proxy: Any, timeout: float) -> socket.socket:
    secure = proxy.scheme == "https"
    sock = socket.create_connection(
        (proxy.hostname, proxy.port or (443 if secure else 8080)), timeout=timeout
    )
    if not secure:
        return sock
    try:
        return ssl.create_default_context().wrap_socket(
            sock, server_hostname=proxy.hostname
        )
    except BaseException:
        sock.close()
        raise


def _open_tunnel(proxy: Any, authority: str, timeout: float) -> socket.socket:
    """A socket that reaches *authority* through a CONNECT tunnel."""
    sock = _connect_proxy(proxy, timeout)
    try:
        lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        lines += [f"{k}: {v}" for k, v in _proxy_auth(proxy).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        response = http.client.HTTPResponse(sock, method="CONNECT")
        response.begin()
        if response.status != 200:
            raise _ProxyRefused(response.status)
        return sock
    except BaseException:
        sock.close()
        raise


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an allowed host cannot bounce to an internal one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "webhook redirects are not followed", headers, fp
        )

ALL_EVENTS = frozenset({
    "action_blocked",
    "budget_warning",
    "budget_exceeded",
    "rate_limited",
    "quota_exceeded",
    "action_error",
    "action_timeout",
    "action_completed",
    "action_deduplicated",
    "action_fallback",
    "action_fallback_failed",
    "approval_denied",
    "output_blocked",
    "anomaly_detected",
    "idempotency_error",
    "audit_log_error",
    "state_backend_error",
    "cost_reconciled",
    "guard_error",
})


@dataclass
class WebhookTarget:
    url: str
    events: set[str] = field(default_factory=lambda: set(ALL_EVENTS))
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 5.0


class EventNotifier:
    """Emits events to registered callbacks and HTTP webhooks.

    Webhooks are delivered by a bounded pool of worker threads (not a new
    thread per event) with retry on failure. Outbound payloads are masked
    when a DataMasker is attached, and webhook URLs are validated to block
    SSRF targets (private / loopback hosts) unless explicitly allowed.

    With private hosts blocked, each delivery attempt resolves the hostname,
    requires every address to be public and connects to those exact addresses,
    which closes DNS rebinding. A configured proxy (``HTTP_PROXY``/``HTTPS_PROXY``,
    honoring ``NO_PROXY``) is used with the same checks: it is asked for the
    validated address, and only if it refuses that (a proxy with a hostname
    allowlist) for the hostname. Pass ``allow_private_hosts=True`` to allow
    private targets; that uses the standard proxy-aware client.
    """

    def __init__(
        self,
        masker: Any = None,
        workers: int = 4,
        max_retries: int = 2,
        allow_private_hosts: bool = False,
        queue_size: int = 1000,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._callbacks: dict[str, list[EventCallback]] = {}
        self._webhooks: list[WebhookTarget] = []
        self._masker = masker
        self._allow_private = allow_private_hosts
        self._ssl_context = ssl_context
        self._max_retries = max_retries
        self._num_workers = workers
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._workers_started = False
        self._lock = threading.Lock()

    def set_masker(self, masker: Any) -> None:
        self._masker = masker

    def on(self, event: str, callback: EventCallback) -> None:
        """Register an in-process callback for an event."""
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    def add_webhook(
        self,
        url: str,
        events: set[str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Register an HTTP webhook endpoint.

        Raises ValueError for non-http(s) schemes or private/loopback hosts
        (SSRF guard), unless the notifier was created with
        ``allow_private_hosts=True``.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Webhook URL must be http(s), got '{parsed.scheme}'")
        if not self._allow_private and _is_private_host(parsed.hostname or ""):
            raise ValueError(
                f"Refusing webhook to private/loopback host '{parsed.hostname}' "
                f"(pass allow_private_hosts=True to override)"
            )
        self._webhooks.append(WebhookTarget(
            url=url,
            events=events or set(ALL_EVENTS),
            headers=headers or {},
            timeout=timeout,
        ))
        self._ensure_workers()

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            for _ in range(self._num_workers):
                threading.Thread(target=self._worker, daemon=True).start()
            self._workers_started = True

    def _worker(self) -> None:
        while True:
            target, payload = self._queue.get()
            try:
                self._send_webhook(target, payload)
            finally:
                self._queue.task_done()

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Fire an event to all matching callbacks and webhooks.

        Callbacks run synchronously; webhooks are queued to the worker pool.
        """
        payload = {"event": event, **data}

        for cb in self._callbacks.get(event, []):
            try:
                cb(event, payload)
            except Exception:
                pass

        for cb in self._callbacks.get("*", []):
            try:
                cb(event, payload)
            except Exception:
                pass

        if not self._webhooks:
            return

        masked = self._mask_payload(payload)
        for wh in self._webhooks:
            if event in wh.events:
                try:
                    self._queue.put_nowait((wh, masked))
                except queue.Full:
                    pass  # best-effort: drop under sustained overload

    def _mask_payload(self, payload: dict) -> dict:
        if self._masker is None:
            return payload
        try:
            return self._masker.mask(payload)
        except Exception:
            # Never send data we failed to mask.
            return {"event": payload.get("event"), "error": "payload masking failed"}

    def _send_webhook(self, target: WebhookTarget, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", **target.headers}
        for attempt in range(self._max_retries + 1):
            try:
                if self._allow_private:
                    self._post_standard(target, body, headers)
                else:
                    self._post_pinned(target, body, headers)
                return
            except _RefusedDelivery:
                return
            except Exception:
                if attempt >= self._max_retries:
                    return
                time.sleep(min(2 ** attempt * 0.1, 2.0))

    def _post_standard(
        self, target: WebhookTarget, body: bytes, headers: dict[str, str]
    ) -> None:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            target.url, data=body, headers=headers, method="POST"
        )
        opener.open(req, timeout=target.timeout)

    def _post_pinned(
        self, target: WebhookTarget, body: bytes, headers: dict[str, str]
    ) -> None:
        parsed = urlparse(target.url)
        host = parsed.hostname or ""
        secure = parsed.scheme == "https"
        port = parsed.port or (443 if secure else 80)
        if not host or _is_private_host(host):
            raise _RefusedDelivery(host)
        proxy = _proxy_for(parsed.scheme, host)
        # Re-resolved on every attempt; only these addresses are connected to.
        try:
            addresses = _resolve_public(host, port)
        except OSError:
            # Networks behind a mandatory proxy often have no external DNS.
            if proxy is None:
                raise
            addresses = []
        if addresses is None:
            raise _RefusedDelivery(host)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if proxy is not None:
            status = self._send_via_proxy(
                proxy, secure, host, port, addresses, path, body, headers, target.timeout
            )
            self._check_status(status)
            return
        if secure:
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host, port, addresses, target.timeout,
                self._ssl_context or ssl.create_default_context(),
            )
        else:
            conn = _PinnedHTTPConnection(host, port, addresses, target.timeout)
        try:
            conn.request("POST", path, body=body, headers=headers)
            status = conn.getresponse().status
        finally:
            conn.close()
        self._check_status(status)

    def _send_via_proxy(
        self, proxy, secure, host, port, addresses, path, body, headers, timeout
    ) -> int:
        """POST through *proxy*: the validated address first, then the hostname
        if the proxy's policy refuses addresses."""
        destinations = [addresses[0][1]] if addresses else []
        destinations.append(host)
        for destination in destinations:
            authority = _authority(destination, port)
            if secure:
                conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                    host, port, [], timeout,
                    self._ssl_context or ssl.create_default_context(),
                    opener=lambda a=authority: _open_tunnel(proxy, a, timeout),
                )
                request_path, request_headers = path, headers
            else:
                conn = _PinnedHTTPConnection(
                    host, port, [], timeout,
                    opener=lambda: _connect_proxy(proxy, timeout),
                )
                request_path = f"http://{authority}{path}"
                request_headers = {
                    "Host": _authority(host, port) if port != 80 else host,
                    **headers,
                    **_proxy_auth(proxy),
                }
            try:
                conn.request("POST", request_path, body=body, headers=request_headers)
                status = conn.getresponse().status
            except _ProxyRefused as e:
                if e.status != 403 or destination == host:
                    raise
                continue
            finally:
                conn.close()
            if status != 403 or destination == host:
                return status
        raise OSError("proxy refused every destination")

    @staticmethod
    def _check_status(status: int) -> None:
        if 300 <= status < 400 or (
            400 <= status < 500 and status not in (408, 425, 429)
        ):
            # Redirects are not followed and client errors will not get better.
            raise _RefusedDelivery(f"HTTP {status}")
        if status >= 400:
            raise OSError(f"webhook delivery failed with HTTP {status}")
