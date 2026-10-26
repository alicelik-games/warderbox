"""P1-7: async pipeline must enforce the same guards as the sync pipeline."""

from __future__ import annotations

import asyncio

from warderbox import (
    AsyncWarderBox,
    CapabilityManager,
    IdempotencyGuard,
)


def _run(coro):
    return asyncio.run(coro)


def test_async_capability_enforced():
    caps = CapabilityManager()
    caps.grant("bot", deny=["delete_record"])
    shield = AsyncWarderBox(budget=5.0, agent_name="bot")
    shield.set_capabilities(caps)

    @shield.async_tool(cost=0.01)
    async def delete_record(id: int) -> str:
        return "DELETED"

    result = _run(delete_record(id=1))
    assert result.blocked is True


def test_async_budget_reservation_no_overspend():
    shield = AsyncWarderBox(budget=0.05)

    @shield.async_tool(cost=0.10)
    async def pricey() -> str:
        return "ran"

    result = _run(pricey())
    assert result.blocked is True
    assert shield.budget_summary["spent"] == 0.0


def test_async_critical_sql_blocked():
    shield = AsyncWarderBox(budget=5.0)

    @shield.async_tool(cost=0.01)
    async def query_db(sql: str) -> str:
        return "EXECUTED"

    assert _run(query_db("DROP TABLE users")).blocked is True


def test_async_idempotency_dedupes():
    guard = IdempotencyGuard()
    guard.add_protected_tool("charge")
    shield = AsyncWarderBox(budget=5.0)
    shield.set_idempotency(guard)

    calls = []

    @shield.async_tool(cost=0.01)
    async def charge(amount: int) -> str:
        calls.append(amount)
        return "charged"

    _run(charge(amount=10))
    second = _run(charge(amount=10))
    assert len(calls) == 1
    assert second.blocked is True


def test_async_estimator_records():
    shield = AsyncWarderBox(budget=5.0)

    @shield.async_tool(cost=0.02)
    async def work() -> str:
        return "ok"

    _run(work())
    assert shield.estimator.stats()["total_data_points"] >= 1
