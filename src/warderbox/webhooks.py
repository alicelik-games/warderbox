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

import ipaddress
import json
import queue
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

EventCallback = Callable[[str, dict[str, Any]], None]


def _is_private_host(host: str) -> bool:
    """True for loopback/private/link-local literals and localhost."""
    if not host:
        return True
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # hostname (not resolved here)
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved

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
    obvious SSRF targets (private / loopback hosts) unless explicitly allowed.
    """

    def __init__(
        self,
        masker: Any = None,
        workers: int = 4,
        max_retries: int = 2,
        allow_private_hosts: bool = False,
        queue_size: int = 1000,
    ) -> None:
        self._callbacks: dict[str, list[EventCallback]] = {}
        self._webhooks: list[WebhookTarget] = []
        self._masker = masker
        self._allow_private = allow_private_hosts
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
            return payload

    def _send_webhook(self, target: WebhookTarget, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", **target.headers}
        for attempt in range(self._max_retries + 1):
            try:
                req = urllib.request.Request(
                    target.url, data=body, headers=headers, method="POST"
                )
                urllib.request.urlopen(req, timeout=target.timeout)
                return
            except Exception:
                if attempt >= self._max_retries:
                    return
                time.sleep(min(2 ** attempt * 0.1, 2.0))
