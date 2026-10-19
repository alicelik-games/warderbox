"""Capability-based permission system for AI agents.

Fine-grained control over which tools each agent can use, with
allowlist/denylist, argument constraints, and time-based access.

Usage:
    caps = CapabilityManager()

    # Allow only specific tools
    caps.grant("researcher", allow=["search", "read_file"])

    # Deny dangerous tools
    caps.grant("intern_bot", deny=["delete_record", "send_email"])

    # Argument constraints
    caps.add_constraint("coder", "run_sql", args_deny=["DROP", "TRUNCATE"])

    # Time-based access
    caps.grant("deploy_bot", allow=["deploy"],
               schedule={"hours": (9, 17), "days": (0, 4)})

    # Check permissions
    allowed, reason = caps.check("researcher", "delete_file")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ToolConstraint:
    args_deny: list[str] = field(default_factory=list)
    args_require: list[str] = field(default_factory=list)
    max_cost: float | None = None
    max_calls_per_hour: int | None = None
    _call_times: list[float] = field(default_factory=list)


@dataclass
class AgentCapability:
    allow: set[str] | None = None
    deny: set[str] = field(default_factory=set)
    constraints: dict[str, ToolConstraint] = field(default_factory=dict)
    schedule: dict | None = None
    _total_calls: int = 0
    _denied_calls: int = 0


class CapabilityManager:
    """Manages per-agent tool permissions."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentCapability] = {}

    def grant(
        self,
        agent_name: str,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        schedule: dict | None = None,
    ) -> None:
        cap = self._agents.get(agent_name, AgentCapability())

        if allow is not None:
            cap.allow = set(allow)
        if deny:
            cap.deny = set(deny)
        if schedule is not None:
            cap.schedule = schedule

        self._agents[agent_name] = cap

    def revoke(self, agent_name: str, tools: list[str] | None = None) -> None:
        if agent_name not in self._agents:
            return
        cap = self._agents[agent_name]
        if tools is None:
            del self._agents[agent_name]
        else:
            for tool in tools:
                if cap.allow is not None:
                    cap.allow.discard(tool)
                cap.deny.add(tool)

    def add_constraint(
        self,
        agent_name: str,
        tool_name: str,
        args_deny: list[str] | None = None,
        args_require: list[str] | None = None,
        max_cost: float | None = None,
        max_calls_per_hour: int | None = None,
    ) -> None:
        if agent_name not in self._agents:
            self._agents[agent_name] = AgentCapability()

        constraint = ToolConstraint(
            args_deny=args_deny or [],
            args_require=args_require or [],
            max_cost=max_cost,
            max_calls_per_hour=max_calls_per_hour,
        )
        self._agents[agent_name].constraints[tool_name] = constraint

    def _check_schedule(self, schedule: dict) -> tuple[bool, str | None]:
        now = datetime.now()
        if "hours" in schedule:
            start_h, end_h = schedule["hours"]
            if not (start_h <= now.hour <= end_h):
                return False, f"Tool only available {start_h}:00-{end_h}:00"

        if "days" in schedule:
            start_d, end_d = schedule["days"]
            if not (start_d <= now.weekday() <= end_d):
                return False, f"Tool only available weekdays {start_d}-{end_d}"

        return True, None

    def _check_constraint(
        self,
        constraint: ToolConstraint,
        tool_name: str,
        args: dict | None = None,
        cost: float = 0.0,
    ) -> tuple[bool, str | None]:
        if constraint.max_cost is not None and cost > constraint.max_cost:
            return False, f"Cost {cost} exceeds max {constraint.max_cost} for '{tool_name}'"

        if constraint.max_calls_per_hour is not None:
            now = time.time()
            constraint._call_times = [
                t for t in constraint._call_times if now - t < 3600
            ]
            if len(constraint._call_times) >= constraint.max_calls_per_hour:
                return False, f"Hourly call limit reached for '{tool_name}'"

        if args:
            args_str = str(args).lower()

            for denied in constraint.args_deny:
                if denied.lower() in args_str:
                    return False, f"Argument contains denied term '{denied}' for '{tool_name}'"

            for required in constraint.args_require:
                if required.lower() not in args_str:
                    return False, f"Missing required term '{required}' for '{tool_name}'"

        return True, None

    def check(
        self,
        agent_name: str,
        tool_name: str,
        args: dict | None = None,
        cost: float = 0.0,
    ) -> tuple[bool, str | None]:
        if agent_name not in self._agents:
            return True, None

        cap = self._agents[agent_name]
        cap._total_calls += 1

        if tool_name in cap.deny:
            cap._denied_calls += 1
            return False, f"Agent '{agent_name}' denied access to '{tool_name}'"

        if cap.allow is not None and tool_name not in cap.allow:
            cap._denied_calls += 1
            return False, f"Agent '{agent_name}' not allowed to use '{tool_name}'"

        if cap.schedule:
            ok, reason = self._check_schedule(cap.schedule)
            if not ok:
                cap._denied_calls += 1
                return False, reason

        if tool_name in cap.constraints:
            constraint = cap.constraints[tool_name]
            ok, reason = self._check_constraint(constraint, tool_name, args, cost)
            if not ok:
                cap._denied_calls += 1
                return False, reason
            constraint._call_times.append(time.time())

        return True, None

    def get_allowed_tools(self, agent_name: str) -> set[str] | None:
        if agent_name not in self._agents:
            return None
        return self._agents[agent_name].allow

    def get_denied_tools(self, agent_name: str) -> set[str]:
        if agent_name not in self._agents:
            return set()
        return set(self._agents[agent_name].deny)

    def stats(self) -> dict[str, dict]:
        result = {}
        for name, cap in self._agents.items():
            result[name] = {
                "allowed_tools": sorted(cap.allow) if cap.allow else None,
                "denied_tools": sorted(cap.deny),
                "constraints": list(cap.constraints.keys()),
                "has_schedule": cap.schedule is not None,
                "total_checks": cap._total_calls,
                "denied_checks": cap._denied_calls,
            }
        return result
