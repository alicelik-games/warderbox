"""Prometheus-compatible metrics export for WarderBox.

Exposes a /metrics endpoint in OpenMetrics/Prometheus text format.
Zero dependencies — uses Python's built-in http.server.

Usage:
    from warderbox.metrics import start_metrics_server

    shield = WarderBox(budget=10.0)
    server = start_metrics_server(shield, port=9090)
    # Scrape http://localhost:9090/metrics with Prometheus

    # Or add metrics endpoint to the dashboard:
    from warderbox.metrics import prometheus_metrics
    text = prometheus_metrics(shield)
"""

from __future__ import annotations

import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from warderbox.shield import WarderBox


def prometheus_metrics(shield: WarderBox) -> str:
    """Generate Prometheus text-format metrics from shield state."""
    stats = shield.stats()
    budget = stats["budget"]
    rl = stats["rate_limits"]

    lines = [
        "# HELP warderbox_actions_total Total number of agent actions.",
        "# TYPE warderbox_actions_total counter",
        f'warderbox_actions_total {stats["total_actions"]}',
        "",
        "# HELP warderbox_actions_blocked_total Total blocked actions.",
        "# TYPE warderbox_actions_blocked_total counter",
        f'warderbox_actions_blocked_total {stats["blocked"]}',
        "",
        "# HELP warderbox_actions_errors_total Total actions with errors.",
        "# TYPE warderbox_actions_errors_total counter",
        f'warderbox_actions_errors_total {stats["errors"]}',
        "",
        "# HELP warderbox_actions_successful_total Total successful actions.",
        "# TYPE warderbox_actions_successful_total counter",
        f'warderbox_actions_successful_total {stats["successful"]}',
        "",
        "# HELP warderbox_budget_spent_dollars Total budget spent.",
        "# TYPE warderbox_budget_spent_dollars gauge",
        f'warderbox_budget_spent_dollars {budget["spent"]}',
        "",
        "# HELP warderbox_budget_remaining_dollars Budget remaining.",
        "# TYPE warderbox_budget_remaining_dollars gauge",
        f'warderbox_budget_remaining_dollars {budget["remaining"] if budget["remaining"] is not None else -1}',
        "",
        "# HELP warderbox_snapshots_count Current number of snapshots.",
        "# TYPE warderbox_snapshots_count gauge",
        f'warderbox_snapshots_count {stats["snapshots"]}',
        "",
        "# HELP warderbox_rate_calls_last_minute Global calls in last minute.",
        "# TYPE warderbox_rate_calls_last_minute gauge",
        f'warderbox_rate_calls_last_minute {rl["global_last_minute"]}',
        "",
        "# HELP warderbox_rate_calls_last_hour Global calls in last hour.",
        "# TYPE warderbox_rate_calls_last_hour gauge",
        f'warderbox_rate_calls_last_hour {rl["global_last_hour"]}',
        "",
    ]

    by_tool = rl.get("by_tool", {})
    if by_tool:
        lines.append("# HELP warderbox_tool_calls_last_minute Per-tool calls in last minute.")
        lines.append("# TYPE warderbox_tool_calls_last_minute gauge")
        for tool_name, tool_stats in by_tool.items():
            lines.append(f'warderbox_tool_calls_last_minute{{tool="{tool_name}"}} {tool_stats["last_minute"]}')
        lines.append("")
        lines.append("# HELP warderbox_tool_calls_last_hour Per-tool calls in last hour.")
        lines.append("# TYPE warderbox_tool_calls_last_hour gauge")
        for tool_name, tool_stats in by_tool.items():
            lines.append(f'warderbox_tool_calls_last_hour{{tool="{tool_name}"}} {tool_stats["last_hour"]}')
        lines.append("")

    return "\n".join(lines) + "\n"


def multiagent_metrics(pool) -> str:
    """Generate Prometheus metrics for an AgentPool."""
    stats = pool.stats()
    lines = [
        "# HELP warderbox_pool_agents_total Number of registered agents.",
        "# TYPE warderbox_pool_agents_total gauge",
        f'warderbox_pool_agents_total {stats["total_agents"]}',
        "",
        "# HELP warderbox_pool_actions_total Total actions across all agents.",
        "# TYPE warderbox_pool_actions_total counter",
        f'warderbox_pool_actions_total {stats["total_actions"]}',
        "",
        "# HELP warderbox_pool_blocked_total Total blocked actions across all agents.",
        "# TYPE warderbox_pool_blocked_total counter",
        f'warderbox_pool_blocked_total {stats["total_blocked"]}',
        "",
        "# HELP warderbox_pool_spent_dollars Total spent across all agents.",
        "# TYPE warderbox_pool_spent_dollars gauge",
        f'warderbox_pool_spent_dollars {stats["total_spent"]}',
        "",
    ]

    for agent_name, agent_stats in stats["per_agent"].items():
        b = agent_stats["budget"]
        lines.append(f'warderbox_agent_actions_total{{agent="{agent_name}"}} {agent_stats["total_actions"]}')
        lines.append(f'warderbox_agent_blocked_total{{agent="{agent_name}"}} {agent_stats["blocked"]}')
        lines.append(f'warderbox_agent_spent_dollars{{agent="{agent_name}"}} {b["spent"]}')

    lines.append("")
    return "\n".join(lines) + "\n"


class _MetricsHandler(BaseHTTPRequestHandler):
    shield: WarderBox

    def do_GET(self):
        if self.path == "/metrics":
            body = prometheus_metrics(self.shield)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_metrics_server(
    shield: WarderBox,
    port: int = 9090,
    host: str = "127.0.0.1",
    background: bool = True,
) -> HTTPServer:
    """Start a Prometheus-compatible metrics server.

    Returns the HTTPServer instance (call .shutdown() to stop).
    """
    handler = type("MetricsHandler", (_MetricsHandler,), {"shield": shield})
    server = HTTPServer((host, port), handler)

    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        server.serve_forever()

    return server
