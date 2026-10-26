"""Fallback tool routing for graceful degradation.

When a tool is unavailable (circuit-broken, timed out, over quota),
automatically routes to a pre-configured cheaper/safer alternative
instead of blocking the agent entirely.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class FallbackRoute:
    """A fallback configuration for a tool."""

    primary_tool: str
    fallback_tool: str
    fallback_fn: Callable | None = None
    fallback_cost: float = 0.0
    max_fallbacks: int | None = None
    condition: str = "any"


class FallbackManager:
    """Manages fallback routing for tools.

    Usage:
        fb = FallbackManager()
        fb.register("gpt4_query", "gpt3_query", fallback_fn=gpt3_fn, fallback_cost=0.001)

        route = fb.get_fallback("gpt4_query")
        if route:
            result = route.fallback_fn(*args)
    """

    def __init__(self) -> None:
        self._routes: dict[str, FallbackRoute] = {}
        self._fallback_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(
        self,
        primary_tool: str,
        fallback_tool: str,
        fallback_fn: Callable | None = None,
        fallback_cost: float = 0.0,
        max_fallbacks: int | None = None,
        condition: str = "any",
    ) -> None:
        """Register a fallback for a primary tool."""
        route = FallbackRoute(
            primary_tool=primary_tool,
            fallback_tool=fallback_tool,
            fallback_fn=fallback_fn,
            fallback_cost=fallback_cost,
            max_fallbacks=max_fallbacks,
            condition=condition,
        )
        with self._lock:
            self._routes[primary_tool] = route
            self._fallback_counts.setdefault(primary_tool, 0)

    def unregister(self, primary_tool: str) -> bool:
        with self._lock:
            return self._routes.pop(primary_tool, None) is not None

    def get_fallback(self, tool_name: str, reason: str = "any") -> FallbackRoute | None:
        """Get a fallback route if available and not exhausted."""
        with self._lock:
            route = self._routes.get(tool_name)
            if route is None:
                return None

            if route.condition != "any" and route.condition != reason:
                return None

            if route.max_fallbacks is not None:
                if self._fallback_counts.get(tool_name, 0) >= route.max_fallbacks:
                    return None

            return route

    def record_fallback(self, tool_name: str) -> None:
        """Record that a fallback was used."""
        with self._lock:
            self._fallback_counts[tool_name] = (
                self._fallback_counts.get(tool_name, 0) + 1
            )

    def reset_count(self, tool_name: str) -> None:
        with self._lock:
            self._fallback_counts[tool_name] = 0

    def has_fallback(self, tool_name: str) -> bool:
        return tool_name in self._routes

    def stats(self) -> dict:
        with self._lock:
            routes_info = {}
            for name, route in self._routes.items():
                routes_info[name] = {
                    "fallback_tool": route.fallback_tool,
                    "fallback_cost": route.fallback_cost,
                    "times_used": self._fallback_counts.get(name, 0),
                    "max_fallbacks": route.max_fallbacks,
                    "has_fn": route.fallback_fn is not None,
                }
            return {
                "registered_routes": len(self._routes),
                "total_fallbacks_used": sum(self._fallback_counts.values()),
                "routes": routes_info,
            }
