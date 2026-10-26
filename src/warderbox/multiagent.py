"""Multi-agent support: isolated shields per agent with a central pool.

Each agent gets its own WarderBox instance with separate budget,
rules, rate limits, and audit trail — while the pool provides a
unified view across all agents.

Usage:
    pool = AgentPool(
        default_budget=5.00,
        global_db_path="all_agents.db",
    )

    # Register agents with custom configs
    pool.register("researcher", budget=3.00)
    pool.register("coder", budget=10.00, mode="test")
    pool.register("reviewer", budget=1.00)

    # Get an agent's shield
    shield = pool.get("researcher")

    @shield.tool(cost=0.05)
    def web_search(query: str) -> str:
        ...

    # Global view
    pool.stats()          # aggregated stats across all agents
    pool.over_budget()    # list of agents over budget
"""

from __future__ import annotations

from typing import Any

from warderbox.ratelimit import ToolLimit
from warderbox.shield import WarderBox


class AgentPool:
    """Manages multiple WarderBox instances, one per named agent."""

    def __init__(
        self,
        default_budget: float | None = None,
        default_mode: str = "production",
        global_db_path: str | None = None,
    ) -> None:
        self._default_budget = default_budget
        self._default_mode = default_mode
        self._global_db_path = global_db_path
        self._agents: dict[str, WarderBox] = {}

    def register(
        self,
        name: str,
        budget: float | None = None,
        mode: str | None = None,
        db_path: str | None = None,
        global_rpm: int | None = None,
        global_rph: int | None = None,
        tool_limits: dict[str, ToolLimit] | None = None,
        config: dict[str, Any] | None = None,
    ) -> WarderBox:
        """Register a new agent with its own isolated shield.

        Returns the created WarderBox instance.
        """
        if name in self._agents:
            raise ValueError(f"Agent '{name}' is already registered")

        if config:
            # Explicit arguments override values from the config dict, so
            # pool.register("x", budget=3, config=cfg) is not silently ignored.
            merged = dict(config)
            if budget is not None:
                merged["budget"] = budget
            if mode is not None:
                merged["mode"] = mode
            if db_path is not None:
                merged["db_path"] = db_path
            if global_rpm is not None:
                merged["global_rpm"] = global_rpm
            if global_rph is not None:
                merged["global_rph"] = global_rph
            shield = WarderBox.from_config(merged)
            shield._agent_name = name
        else:
            shield = WarderBox(
                budget=budget if budget is not None else self._default_budget,
                mode=mode or self._default_mode,
                db_path=db_path or self._global_db_path,
                global_rpm=global_rpm,
                global_rph=global_rph,
                tool_limits=tool_limits,
                agent_name=name,
            )

        self._agents[name] = shield
        return shield

    def get(self, name: str) -> WarderBox:
        """Get an agent's shield by name."""
        if name not in self._agents:
            raise KeyError(f"Agent '{name}' not registered. Use pool.register('{name}') first.")
        return self._agents[name]

    def __getitem__(self, name: str) -> WarderBox:
        return self.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    @property
    def agents(self) -> list[str]:
        """List of registered agent names."""
        return list(self._agents.keys())

    def stats(self) -> dict[str, Any]:
        """Aggregated stats across all agents."""
        total_actions = 0
        total_blocked = 0
        total_errors = 0
        total_spent = 0.0
        per_agent = {}

        for name, shield in self._agents.items():
            s = shield.stats()
            total_actions += s["total_actions"]
            total_blocked += s["blocked"]
            total_errors += s["errors"]
            total_spent += s["budget"]["spent"]
            per_agent[name] = s

        return {
            "total_agents": len(self._agents),
            "total_actions": total_actions,
            "total_blocked": total_blocked,
            "total_errors": total_errors,
            "total_spent": round(total_spent, 4),
            "per_agent": per_agent,
        }

    def over_budget(self) -> list[str]:
        """Return names of agents that have exceeded their budget."""
        return [
            name for name, shield in self._agents.items()
            if shield._budget.over_budget
        ]

    def budget_warnings(self) -> list[str]:
        """Return names of agents near their budget limit (>80%)."""
        return [
            name for name, shield in self._agents.items()
            if shield._budget.warning
        ]

    def history(self, agent_name: str | None = None) -> list[dict]:
        """Get action history for a specific agent or all agents."""
        if agent_name:
            return self.get(agent_name).history()

        all_history = []
        for name, shield in self._agents.items():
            for entry in shield.history():
                entry["agent"] = name
                all_history.append(entry)

        all_history.sort(key=lambda x: x["timestamp"])
        return all_history

    def shutdown(self) -> None:
        """Close all logger connections."""
        for shield in self._agents.values():
            if shield._logger:
                shield._logger.close()
