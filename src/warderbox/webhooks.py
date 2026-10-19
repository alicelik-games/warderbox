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
    notifier.add_webhook("action_blocked", "https://hooks.slack.com/...")

    notifier.emit("action_blocked", {"tool": "run_sql", "reason": "DROP TABLE"})
"""

from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


EventCallback = Callable[[str, dict[str, Any]], None]

ALL_EVENTS = frozenset({
    "action_blocked",
    "budget_warning",
    "budget_exceeded",
    "rate_limited",
    "action_error",
    "action_completed",
})


@dataclass
class WebhookTarget:
    url: str
    events: set[str] = field(default_factory=lambda: set(ALL_EVENTS))
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 5.0


class EventNotifier:
    """Emits events to registered callbacks and HTTP webhooks."""

    def __init__(self) -> None:
        self._callbacks: dict[str, list[EventCallback]] = {}
        self._webhooks: list[WebhookTarget] = []

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
        """Register an HTTP webhook endpoint."""
        self._webhooks.append(WebhookTarget(
            url=url,
            events=events or set(ALL_EVENTS),
            headers=headers or {},
            timeout=timeout,
        ))

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Fire an event to all matching callbacks and webhooks.

        Callbacks run synchronously; webhooks fire in background threads.
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

        for wh in self._webhooks:
            if event in wh.events:
                thread = threading.Thread(
                    target=self._send_webhook,
                    args=(wh, payload),
                    daemon=True,
                )
                thread.start()

    def _send_webhook(self, target: WebhookTarget, payload: dict) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", **target.headers}
            req = urllib.request.Request(
                target.url, data=body, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=target.timeout)
        except Exception:
            pass
