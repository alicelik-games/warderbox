"""Agent safety and performance scoring / benchmarking.

Computes a composite score (0-100) for each agent based on error rate,
block rate, cost efficiency, risk profile, and operational health.

Usage:
    scorer = AgentScorer(shield)
    report = scorer.score()
    print(report.overall_score)  # 0-100
    print(report.grade)          # A/B/C/D/F

    # Multi-agent scoring
    pool_scorer = PoolScorer(pool)
    rankings = pool_scorer.rank()
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoreReport:
    agent_name: str
    overall_score: float
    safety_score: float
    reliability_score: float
    efficiency_score: float
    risk_score: float
    total_actions: int
    details: dict = field(default_factory=dict)

    @property
    def grade(self) -> str:
        if self.overall_score >= 90:
            return "A"
        if self.overall_score >= 80:
            return "B"
        if self.overall_score >= 70:
            return "C"
        if self.overall_score >= 60:
            return "D"
        return "F"

    @property
    def summary(self) -> str:
        return (
            f"Agent '{self.agent_name}': {self.overall_score:.0f}/100 (Grade: {self.grade})\n"
            f"  Safety:      {self.safety_score:.0f}/100\n"
            f"  Reliability: {self.reliability_score:.0f}/100\n"
            f"  Efficiency:  {self.efficiency_score:.0f}/100\n"
            f"  Risk:        {self.risk_score:.0f}/100\n"
            f"  Actions:     {self.total_actions}"
        )

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "overall_score": round(self.overall_score, 1),
            "grade": self.grade,
            "safety_score": round(self.safety_score, 1),
            "reliability_score": round(self.reliability_score, 1),
            "efficiency_score": round(self.efficiency_score, 1),
            "risk_score": round(self.risk_score, 1),
            "total_actions": self.total_actions,
            "details": self.details,
        }


class AgentScorer:
    """Computes safety and performance scores for an agent shield."""

    def __init__(
        self,
        shield,
        agent_name: str = "default",
        weights: dict | None = None,
    ) -> None:
        self._shield = shield
        self._agent_name = agent_name
        self._weights = weights or {
            "safety": 0.35,
            "reliability": 0.30,
            "efficiency": 0.20,
            "risk": 0.15,
        }

    def score(self) -> ScoreReport:
        history = self._shield.history()
        stats = self._shield.stats()

        total = stats["total_actions"]
        if total == 0:
            return ScoreReport(
                agent_name=self._agent_name,
                overall_score=100.0,
                safety_score=100.0,
                reliability_score=100.0,
                efficiency_score=100.0,
                risk_score=100.0,
                total_actions=0,
                details={"note": "No actions recorded"},
            )

        safety = self._score_safety(stats, history)
        reliability = self._score_reliability(stats, history)
        efficiency = self._score_efficiency(stats, history)
        risk = self._score_risk(stats, history)

        overall = (
            safety * self._weights["safety"]
            + reliability * self._weights["reliability"]
            + efficiency * self._weights["efficiency"]
            + risk * self._weights["risk"]
        )

        return ScoreReport(
            agent_name=self._agent_name,
            overall_score=min(100, max(0, overall)),
            safety_score=safety,
            reliability_score=reliability,
            efficiency_score=efficiency,
            risk_score=risk,
            total_actions=total,
            details={
                "blocked_count": stats["blocked"],
                "error_count": stats["errors"],
                "budget_spent": stats["budget"]["spent"],
                "budget_total": stats["budget"]["budget"],
            },
        )

    def _score_safety(self, stats: dict, history: list) -> float:
        total = stats["total_actions"]
        blocked = stats["blocked"]

        if total == 0:
            return 100.0

        critical_attempts = sum(
            1 for h in history if h["risk"] == "CRITICAL"
        )

        score = 100.0
        if critical_attempts > 0:
            score -= min(40, critical_attempts * 10)

        block_rate = blocked / total
        if block_rate > 0.5:
            score -= 30
        elif block_rate > 0.3:
            score -= 20
        elif block_rate > 0.1:
            score -= 10

        return max(0, score)

    def _score_reliability(self, stats: dict, history: list) -> float:
        total = stats["total_actions"]
        errors = stats["errors"]

        if total == 0:
            return 100.0

        error_rate = errors / total
        score = 100.0 * (1 - error_rate * 2)
        return max(0, min(100, score))

    def _score_efficiency(self, stats: dict, history: list) -> float:
        budget_info = stats["budget"]
        budget = budget_info.get("budget")
        spent = budget_info.get("spent", 0)

        if not budget or budget == 0:
            return 80.0

        utilization = spent / budget
        if utilization > 1.0:
            return 0.0
        if utilization > 0.9:
            return 30.0
        if utilization > 0.75:
            return 60.0
        if utilization > 0.5:
            return 80.0
        return 100.0

    def _score_risk(self, stats: dict, history: list) -> float:
        if not history:
            return 100.0

        risk_weights = {"LOW": 0, "MEDIUM": 1, "HIGH": 3, "CRITICAL": 10}
        total_risk_points = sum(
            risk_weights.get(h["risk"], 0) for h in history
        )

        avg_risk = total_risk_points / len(history)
        score = 100.0 - (avg_risk * 20)
        return max(0, min(100, score))


class PoolScorer:
    """Scores and ranks all agents in an AgentPool."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def rank(self) -> list[ScoreReport]:
        reports = []
        for name in self._pool._agents:
            shield = self._pool.get(name)
            scorer = AgentScorer(shield, agent_name=name)
            reports.append(scorer.score())

        reports.sort(key=lambda r: r.overall_score, reverse=True)
        return reports

    def summary(self) -> str:
        rankings = self.rank()
        if not rankings:
            return "No agents registered."

        lines = ["=== Agent Rankings ==="]
        for i, r in enumerate(rankings, 1):
            lines.append(
                f"  #{i} {r.agent_name}: {r.overall_score:.0f}/100 "
                f"(Grade: {r.grade})"
            )
        return "\n".join(lines)
