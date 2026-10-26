"""Regression tests for compliance (P1-3), forecast (P1-4), scoring (P1-5)."""

from __future__ import annotations

from warderbox import WarderBox
from warderbox.compliance import ComplianceReporter
from warderbox.forecast import CostForecaster
from warderbox.scoring import AgentScorer


def _shield_with_actions(db_path: str) -> WarderBox:
    shield = WarderBox(budget=5.0, db_path=db_path)

    @shield.tool(cost=0.02)
    def safe(q: str) -> str:
        return "ok"

    @shield.tool(cost=0.01)
    def query_db(sql: str) -> str:
        return "ran"

    safe(q="hello")
    safe(q="world")
    query_db("DROP TABLE users")  # blocked, CRITICAL
    return shield


# --- P1-3 compliance ---------------------------------------------------------

def test_p1_3_date_filter_includes_rows(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = _shield_with_actions(db)
    shield._logger.close()

    reporter = ComplianceReporter(db)
    full = reporter.generate_report()
    ranged = reporter.generate_report(start="2020-01-01", end="2099-12-31")
    assert full.total_actions == 3
    assert ranged.total_actions == 3


def test_p1_3_critical_anomaly_detected(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = _shield_with_actions(db)
    shield._logger.close()

    report = ComplianceReporter(db).generate_report()
    assert any("CRITICAL" in a for a in report.anomalies)


def test_p1_3_fresh_db_no_crash(tmp_path):
    db = str(tmp_path / "fresh.db")
    report = ComplianceReporter(db).generate_report()
    assert report.total_actions == 0


def test_p1_3_html_escapes_tool_names(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = WarderBox(budget=5.0, db_path=db)

    # A tool name carrying an injection payload (as could arrive from an
    # untrusted agent) must be HTML-escaped in the report.
    from warderbox.action import Action, ActionResult
    evil = "<script>alert(1)</script>"
    shield._logger.log(ActionResult(action=Action(tool_name=evil, args={})))
    shield._logger.close()

    html_out = ComplianceReporter(db).to_html()
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


# --- P1-4 forecast -----------------------------------------------------------

def test_p1_4_burn_rate_reasonable(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = _shield_with_actions(db)

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    # Must not explode into millions-per-hour from a microsecond window.
    assert report.burn_rate_per_hour < 1_000_000
    assert report.time_window_seconds >= 0


# --- P1-5 scoring ------------------------------------------------------------

def test_p1_5_critical_attempt_penalized(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = _shield_with_actions(db)

    scorer = AgentScorer(shield)
    card = scorer.score()
    # risk + safety dimensions must drop when a CRITICAL action was attempted
    assert card.risk_score < 100
    assert card.safety_score < 100
