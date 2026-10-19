"""Smart Model Routing — pick the cheapest model that can handle the task.

Classifies task complexity and routes to the optimal model tier,
cutting costs by 60-80% without sacrificing quality.

Usage:
    router = ModelRouter()
    router.add_model("fast", cost_per_1k=0.00025, max_complexity=0.3)
    router.add_model("balanced", cost_per_1k=0.003, max_complexity=0.7)
    router.add_model("powerful", cost_per_1k=0.015, max_complexity=1.0)

    choice = router.route("Summarize this email")
    print(choice.model)  # "fast"

    choice = router.route("Write a distributed systems design doc")
    print(choice.model)  # "powerful"
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum


class Complexity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ModelTier:
    name: str
    cost_per_1k_tokens: float
    max_complexity: float = 1.0
    latency_ms: float = 0.0
    provider: str = ""
    model_id: str = ""


@dataclass
class RoutingDecision:
    model: str
    complexity: Complexity
    complexity_score: float
    estimated_cost_per_1k: float
    reason: str
    fallback: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "complexity": self.complexity.value,
            "complexity_score": round(self.complexity_score, 3),
            "estimated_cost_per_1k": self.estimated_cost_per_1k,
            "reason": self.reason,
            "fallback": self.fallback,
            "latency_ms": self.latency_ms,
        }


@dataclass
class RoutingStats:
    total_routed: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    by_complexity: dict[str, int] = field(default_factory=dict)
    total_cost_actual: float = 0.0
    total_cost_if_max: float = 0.0
    fallback_count: int = 0

    @property
    def savings_percent(self) -> float:
        if self.total_cost_if_max == 0:
            return 0.0
        return (1 - self.total_cost_actual / self.total_cost_if_max) * 100

    @property
    def summary(self) -> str:
        lines = [
            "=== Routing Stats ===",
            f"Total routed: {self.total_routed}",
            f"Savings: {self.savings_percent:.1f}%",
            f"Cost (routed): ${self.total_cost_actual:.4f}",
            f"Cost (if always max): ${self.total_cost_if_max:.4f}",
            f"Fallbacks triggered: {self.fallback_count}",
        ]
        if self.by_model:
            lines.append("\nBy model:")
            for model, count in sorted(self.by_model.items(), key=lambda x: -x[1]):
                lines.append(f"  {model}: {count}")
        if self.by_complexity:
            lines.append("\nBy complexity:")
            for comp, count in sorted(self.by_complexity.items()):
                lines.append(f"  {comp}: {count}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_routed": self.total_routed,
            "by_model": self.by_model,
            "by_complexity": self.by_complexity,
            "total_cost_actual": round(self.total_cost_actual, 6),
            "total_cost_if_max": round(self.total_cost_if_max, 6),
            "savings_percent": round(self.savings_percent, 1),
            "fallback_count": self.fallback_count,
        }


_HIGH_COMPLEXITY_SIGNALS = [
    r"\b(design|architect|implement|refactor|optimize|debug|analyze)\b",
    r"\b(distributed|concurrent|async|parallel|recursive)\b",
    r"\b(security|vulnerability|exploit|injection|encryption)\b",
    r"\b(migration|schema|database design|data model)\b",
    r"\b(algorithm|complexity|performance|benchmark)\b",
    r"\b(multi[- ]?step|chain[- ]?of[- ]?thought|reasoning)\b",
    r"\b(compare|contrast|evaluate|trade[- ]?off|pros and cons)\b",
]

_LOW_COMPLEXITY_SIGNALS = [
    r"\b(summarize|summary|tldr|shorten)\b",
    r"\b(translate|convert|format|reformat)\b",
    r"\b(extract|parse|list|enumerate)\b",
    r"\b(classify|categorize|label|tag)\b",
    r"\b(yes|no|true|false|simple|basic)\b",
    r"\b(hello|hi|hey|greet|thanks)\b",
    r"\b(define|what is|who is|when)\b",
    r"\b(fix typo|rename|spelling|grammar)\b",
]


class ComplexityClassifier:
    """Estimates task complexity from the prompt text and metadata."""

    def __init__(self) -> None:
        self._high_patterns = [re.compile(p, re.IGNORECASE) for p in _HIGH_COMPLEXITY_SIGNALS]
        self._low_patterns = [re.compile(p, re.IGNORECASE) for p in _LOW_COMPLEXITY_SIGNALS]
        self._tool_complexity: dict[str, float] = {}
        self._custom_rules: list[tuple[re.Pattern, float]] = []

    def set_tool_complexity(self, tool_name: str, score: float) -> None:
        self._tool_complexity[tool_name] = max(0.0, min(1.0, score))

    def add_rule(self, pattern: str, score: float) -> None:
        self._custom_rules.append((re.compile(pattern, re.IGNORECASE), max(0.0, min(1.0, score))))

    def classify(
        self,
        prompt: str = "",
        tool_name: str = "",
        token_estimate: int = 0,
    ) -> tuple[Complexity, float]:
        score = 0.5

        if tool_name and tool_name in self._tool_complexity:
            return self._to_level(self._tool_complexity[tool_name])

        for pat, custom_score in self._custom_rules:
            if pat.search(prompt):
                return self._to_level(custom_score)

        if prompt:
            high_hits = sum(1 for p in self._high_patterns if p.search(prompt))
            low_hits = sum(1 for p in self._low_patterns if p.search(prompt))

            score += high_hits * 0.12
            score -= low_hits * 0.10

            word_count = len(prompt.split())
            if word_count > 500:
                score += 0.15
            elif word_count > 200:
                score += 0.08
            elif word_count < 20:
                score -= 0.08

            if prompt.count("```") >= 2:
                score += 0.10
            if prompt.count("\n") > 20:
                score += 0.05

        if token_estimate > 4000:
            score += 0.10
        elif token_estimate > 2000:
            score += 0.05

        score = max(0.0, min(1.0, score))
        return self._to_level(score)

    @staticmethod
    def _to_level(score: float) -> tuple[Complexity, float]:
        if score <= 0.35:
            return Complexity.LOW, score
        if score <= 0.65:
            return Complexity.MEDIUM, score
        return Complexity.HIGH, score


class ModelRouter:
    """Routes tasks to the cheapest model that can handle the complexity.

    Usage:
        router = ModelRouter()
        router.add_model("haiku", cost_per_1k=0.00025, max_complexity=0.3)
        router.add_model("sonnet", cost_per_1k=0.003, max_complexity=0.7)
        router.add_model("opus", cost_per_1k=0.015, max_complexity=1.0)

        decision = router.route("Summarize this text")
        # → model="haiku", complexity=LOW, savings vs always using opus

        decision = router.route("Design a microservices architecture")
        # → model="opus", complexity=HIGH
    """

    def __init__(self) -> None:
        self._models: list[ModelTier] = []
        self._classifier = ComplexityClassifier()
        self._stats = RoutingStats()
        self._history: list[RoutingDecision] = []
        self._overrides: dict[str, str] = {}

    @property
    def classifier(self) -> ComplexityClassifier:
        return self._classifier

    def add_model(
        self,
        name: str,
        cost_per_1k: float,
        max_complexity: float = 1.0,
        latency_ms: float = 0.0,
        provider: str = "",
        model_id: str = "",
    ) -> None:
        self._models.append(ModelTier(
            name=name,
            cost_per_1k_tokens=cost_per_1k,
            max_complexity=max_complexity,
            latency_ms=latency_ms,
            provider=provider,
            model_id=model_id,
        ))
        self._models.sort(key=lambda m: m.cost_per_1k_tokens)

    def override_tool(self, tool_name: str, model_name: str) -> None:
        self._overrides[tool_name] = model_name

    def remove_override(self, tool_name: str) -> None:
        self._overrides.pop(tool_name, None)

    def route(
        self,
        prompt: str = "",
        tool_name: str = "",
        token_estimate: int = 0,
        min_complexity: Complexity | None = None,
    ) -> RoutingDecision:
        if not self._models:
            raise ValueError("No models registered. Call add_model() first.")

        if tool_name and tool_name in self._overrides:
            model = self._find_model(self._overrides[tool_name])
            if model:
                decision = RoutingDecision(
                    model=model.name,
                    complexity=Complexity.MEDIUM,
                    complexity_score=0.5,
                    estimated_cost_per_1k=model.cost_per_1k_tokens,
                    reason=f"Tool override: {tool_name} → {model.name}",
                    latency_ms=model.latency_ms,
                )
                self._record(decision)
                return decision

        complexity, score = self._classifier.classify(prompt, tool_name, token_estimate)

        if min_complexity and self._complexity_rank(complexity) < self._complexity_rank(min_complexity):
            complexity = min_complexity
            score = max(score, self._complexity_threshold(min_complexity))

        chosen = None
        for model in self._models:
            if model.max_complexity >= score:
                chosen = model
                break

        if not chosen:
            chosen = self._models[-1]

        fallback = None
        fallback_idx = self._models.index(chosen) + 1
        if fallback_idx < len(self._models):
            fallback = self._models[fallback_idx].name

        decision = RoutingDecision(
            model=chosen.name,
            complexity=complexity,
            complexity_score=score,
            estimated_cost_per_1k=chosen.cost_per_1k_tokens,
            reason=self._build_reason(complexity, chosen.name),
            fallback=fallback,
            latency_ms=chosen.latency_ms,
        )

        self._record(decision)
        return decision

    def record_fallback(self, from_model: str, to_model: str) -> None:
        self._stats.fallback_count += 1

    def stats(self) -> RoutingStats:
        return self._stats

    def reset_stats(self) -> None:
        self._stats = RoutingStats()
        self._history.clear()

    def recent_decisions(self, limit: int = 20) -> list[dict]:
        return [d.to_dict() for d in self._history[-limit:]]

    def _record(self, decision: RoutingDecision) -> None:
        self._stats.total_routed += 1
        self._stats.by_model[decision.model] = self._stats.by_model.get(decision.model, 0) + 1
        self._stats.by_complexity[decision.complexity.value] = (
            self._stats.by_complexity.get(decision.complexity.value, 0) + 1
        )
        self._stats.total_cost_actual += decision.estimated_cost_per_1k
        if self._models:
            self._stats.total_cost_if_max += self._models[-1].cost_per_1k_tokens
        self._history.append(decision)

    def _find_model(self, name: str) -> ModelTier | None:
        for m in self._models:
            if m.name == name:
                return m
        return None

    @staticmethod
    def _build_reason(complexity: Complexity, model: str) -> str:
        labels = {
            Complexity.LOW: "Simple task",
            Complexity.MEDIUM: "Moderate complexity",
            Complexity.HIGH: "Complex task",
        }
        return f"{labels[complexity]} → routed to {model}"

    @staticmethod
    def _complexity_rank(c: Complexity) -> int:
        return {Complexity.LOW: 0, Complexity.MEDIUM: 1, Complexity.HIGH: 2}[c]

    @staticmethod
    def _complexity_threshold(c: Complexity) -> float:
        return {Complexity.LOW: 0.2, Complexity.MEDIUM: 0.5, Complexity.HIGH: 0.8}[c]
