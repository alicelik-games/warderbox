"""Compliance and audit reporting for AI agent operations.

Generates structured reports from SQLite action logs for SOC2, GDPR,
and internal audit purposes.

Usage:
    reporter = ComplianceReporter(db_path="audit.db")
    report = reporter.generate_report()
    print(report.summary)

    # Export as JSON or HTML
    reporter.export_json("audit_report.json")
    html = reporter.to_html()
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AuditReport:
    generated_at: str
    period_start: str | None
    period_end: str | None
    total_actions: int = 0
    blocked_actions: int = 0
    error_actions: int = 0
    successful_actions: int = 0
    total_cost: float = 0.0
    risk_breakdown: dict[str, int] = field(default_factory=dict)
    tool_usage: dict[str, int] = field(default_factory=dict)
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    top_expensive_tools: list[dict] = field(default_factory=list)
    hourly_distribution: dict[str, int] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)

    @property
    def block_rate(self) -> float:
        if self.total_actions == 0:
            return 0.0
        return self.blocked_actions / self.total_actions

    @property
    def error_rate(self) -> float:
        if self.total_actions == 0:
            return 0.0
        return self.error_actions / self.total_actions

    @property
    def summary(self) -> str:
        lines = [
            f"=== WarderBox Audit Report ===",
            f"Generated: {self.generated_at}",
            f"Period: {self.period_start or 'all'} — {self.period_end or 'now'}",
            f"",
            f"Total actions:    {self.total_actions}",
            f"Successful:       {self.successful_actions}",
            f"Blocked:          {self.blocked_actions} ({self.block_rate:.1%})",
            f"Errors:           {self.error_actions} ({self.error_rate:.1%})",
            f"Total cost:       ${self.total_cost:.2f}",
            f"",
            f"Risk breakdown:",
        ]
        for risk, count in sorted(self.risk_breakdown.items()):
            lines.append(f"  {risk}: {count}")

        if self.top_expensive_tools:
            lines.append("")
            lines.append("Top costly tools:")
            for t in self.top_expensive_tools[:5]:
                lines.append(f"  {t['tool']}: ${t['cost']:.2f} ({t['calls']} calls)")

        if self.anomalies:
            lines.append("")
            lines.append("Anomalies detected:")
            for a in self.anomalies:
                lines.append(f"  - {a}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_actions": self.total_actions,
            "blocked_actions": self.blocked_actions,
            "error_actions": self.error_actions,
            "successful_actions": self.successful_actions,
            "total_cost": self.total_cost,
            "block_rate": round(self.block_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "risk_breakdown": self.risk_breakdown,
            "tool_usage": self.tool_usage,
            "blocked_reasons": self.blocked_reasons,
            "top_expensive_tools": self.top_expensive_tools,
            "hourly_distribution": self.hourly_distribution,
            "anomalies": self.anomalies,
        }


class ComplianceReporter:
    """Generates audit reports from WarderBox SQLite logs."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def generate_report(
        self,
        start: str | None = None,
        end: str | None = None,
    ) -> AuditReport:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            return self._build_report(conn, start, end)
        finally:
            conn.close()

    def _build_report(
        self,
        conn: sqlite3.Connection,
        start: str | None,
        end: str | None,
    ) -> AuditReport:
        where_parts = []
        params: list = []
        if start:
            where_parts.append("timestamp >= ?")
            params.append(start)
        if end:
            where_parts.append("timestamp <= ?")
            params.append(end)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        report = AuditReport(
            generated_at=datetime.now().isoformat(),
            period_start=start,
            period_end=end,
        )

        row = conn.execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN blocked = 1 THEN 1 ELSE 0 END) as blocked, "
            f"SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) as errors, "
            f"COALESCE(SUM(cost), 0) as total_cost "
            f"FROM actions {where}",
            params,
        ).fetchone()

        report.total_actions = row["total"]
        report.blocked_actions = row["blocked"] or 0
        report.error_actions = row["errors"] or 0
        report.successful_actions = (
            report.total_actions - report.blocked_actions - report.error_actions
        )
        report.total_cost = row["total_cost"]

        for r in conn.execute(
            f"SELECT risk, COUNT(*) as cnt FROM actions {where} GROUP BY risk",
            params,
        ):
            report.risk_breakdown[r["risk"] or "UNKNOWN"] = r["cnt"]

        for r in conn.execute(
            f"SELECT tool_name, COUNT(*) as cnt FROM actions {where} "
            f"GROUP BY tool_name ORDER BY cnt DESC",
            params,
        ):
            report.tool_usage[r["tool_name"]] = r["cnt"]

        for r in conn.execute(
            f"SELECT block_reason, COUNT(*) as cnt FROM actions "
            f"{where + ' AND' if where else 'WHERE'} blocked = 1 "
            f"GROUP BY block_reason ORDER BY cnt DESC",
            params,
        ):
            report.blocked_reasons[r["block_reason"] or "Unknown"] = r["cnt"]

        for r in conn.execute(
            f"SELECT tool_name, SUM(cost) as total_cost, COUNT(*) as calls "
            f"FROM actions {where} GROUP BY tool_name "
            f"ORDER BY total_cost DESC LIMIT 10",
            params,
        ):
            report.top_expensive_tools.append({
                "tool": r["tool_name"],
                "cost": r["total_cost"] or 0,
                "calls": r["calls"],
            })

        for r in conn.execute(
            f"SELECT substr(timestamp, 12, 2) as hour, COUNT(*) as cnt "
            f"FROM actions {where} GROUP BY hour ORDER BY hour",
            params,
        ):
            report.hourly_distribution[r["hour"] or "??"] = r["cnt"]

        report.anomalies = self._detect_anomalies(report)

        return report

    def _detect_anomalies(self, report: AuditReport) -> list[str]:
        anomalies = []

        if report.block_rate > 0.3:
            anomalies.append(
                f"High block rate ({report.block_rate:.0%}) — "
                f"agent may be attempting many risky operations"
            )

        if report.error_rate > 0.2:
            anomalies.append(
                f"High error rate ({report.error_rate:.0%}) — "
                f"tools may be misconfigured or unavailable"
            )

        critical = report.risk_breakdown.get("CRITICAL", 0)
        if critical > 0:
            anomalies.append(
                f"{critical} CRITICAL-risk actions attempted"
            )

        if report.total_cost > 100:
            anomalies.append(
                f"High total cost (${report.total_cost:.2f})"
            )

        return anomalies

    def export_json(self, path: str, **kwargs) -> None:
        report = self.generate_report(**kwargs)
        with open(path, "w") as f:
            json.dump(report.to_dict(), f, indent=2)

    def to_html(self, **kwargs) -> str:
        report = self.generate_report(**kwargs)
        d = report.to_dict()

        risk_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in d["risk_breakdown"].items()
        )
        tool_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in list(d["tool_usage"].items())[:10]
        )
        blocked_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in d["blocked_reasons"].items()
        )
        anomaly_items = "".join(
            f"<li>{a}</li>" for a in d["anomalies"]
        )

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>WarderBox Audit Report</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 0.5rem; }}
h2 {{ color: #8b949e; margin-top: 2rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin: 1rem 0; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem; text-align: center; }}
.card .value {{ font-size: 2rem; font-weight: bold; color: #58a6ff; }}
.card .label {{ color: #8b949e; font-size: 0.85rem; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
th, td {{ text-align: left; padding: 0.5rem; border-bottom: 1px solid #30363d; }}
th {{ color: #8b949e; }}
.anomaly {{ background: #3d1f00; border-left: 3px solid #d29922; padding: 0.5rem 1rem; margin: 0.5rem 0; border-radius: 4px; }}
.anomaly li {{ margin: 0.3rem 0; }}
</style>
</head>
<body>
<h1>WarderBox Audit Report</h1>
<p>Generated: {d['generated_at']}<br>
Period: {d['period_start'] or 'all'} &mdash; {d['period_end'] or 'now'}</p>

<div class="cards">
  <div class="card"><div class="value">{d['total_actions']}</div><div class="label">Total Actions</div></div>
  <div class="card"><div class="value">{d['successful_actions']}</div><div class="label">Successful</div></div>
  <div class="card"><div class="value">{d['blocked_actions']}</div><div class="label">Blocked</div></div>
  <div class="card"><div class="value">{d['error_actions']}</div><div class="label">Errors</div></div>
  <div class="card"><div class="value">${d['total_cost']:.2f}</div><div class="label">Total Cost</div></div>
  <div class="card"><div class="value">{d['block_rate']:.0%}</div><div class="label">Block Rate</div></div>
</div>

<h2>Risk Breakdown</h2>
<table><tr><th>Risk Level</th><th>Count</th></tr>{risk_rows}</table>

<h2>Tool Usage (Top 10)</h2>
<table><tr><th>Tool</th><th>Calls</th></tr>{tool_rows}</table>

{"<h2>Blocked Reasons</h2><table><tr><th>Reason</th><th>Count</th></tr>" + blocked_rows + "</table>" if blocked_rows else ""}

{"<h2>Anomalies</h2><div class='anomaly'><ul>" + anomaly_items + "</ul></div>" if anomaly_items else ""}

</body>
</html>"""
