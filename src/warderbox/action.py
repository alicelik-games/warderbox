"""Action model — represents a single agent action with metadata."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Action:
    """A single action an agent wants to perform."""

    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    risk: RiskLevel = RiskLevel.LOW
    cost: float = 0.0

    @property
    def description(self) -> str:
        args_str = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.tool_name}({args_str})"


@dataclass
class ActionResult:
    """The result of executing an action."""

    action: Action
    output: Any = None
    error: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    snapshot_id: str | None = None
    cost: float = 0.0
    reversible: bool | None = None
    # Set when this result came from a circuit-breaker fallback route. The
    # primary action remains action.tool_name; this records what actually ran.
    fallback_tool: str | None = None

    @property
    def success(self) -> bool:
        return not self.blocked and self.error is None
