"""Tests for rollback transparency — reversible flag on tool decorator."""

from warderbox import WarderBox


class TestReversibility:
    def test_default_reversible_is_none(self):
        shield = WarderBox()

        @shield.tool(cost=0.01)
        def plain_tool(x: int) -> int:
            return x * 2

        result = plain_tool(5)
        assert result.reversible is None

    def test_explicit_reversible_true(self):
        shield = WarderBox()

        @shield.tool(cost=0.01, reversible=True)
        def safe_tool(x: int) -> int:
            return x + 1

        result = safe_tool(3)
        assert result.reversible is True

    def test_explicit_reversible_false(self):
        shield = WarderBox()

        @shield.tool(cost=0.01, reversible=False)
        def dangerous_tool(x: int) -> int:
            return x * 10

        result = dangerous_tool(2)
        assert result.reversible is False

    def test_auto_reversible_with_snapshot(self):
        state = {"value": 0}
        shield = WarderBox()

        @shield.tool(
            cost=0.01,
            snapshot_state=lambda: state.copy(),
            restore_state=lambda s: state.update(s),
        )
        def stateful_tool(x: int) -> int:
            state["value"] = x
            return x

        result = stateful_tool(42)
        assert result.reversible is True

    def test_explicit_false_overrides_snapshot(self):
        state = {"value": 0}
        shield = WarderBox()

        @shield.tool(
            cost=0.01,
            reversible=False,
            snapshot_state=lambda: state.copy(),
            restore_state=lambda s: state.update(s),
        )
        def tool_with_side_effects(x: int) -> int:
            state["value"] = x
            return x

        result = tool_with_side_effects(10)
        assert result.reversible is False

    def test_history_includes_reversible(self):
        shield = WarderBox()

        @shield.tool(cost=0.01, reversible=True)
        def rev_tool(x: int) -> int:
            return x

        @shield.tool(cost=0.01, reversible=False)
        def irrev_tool(x: int) -> int:
            return x

        @shield.tool(cost=0.01)
        def unknown_tool(x: int) -> int:
            return x

        rev_tool(1)
        irrev_tool(2)
        unknown_tool(3)

        hist = shield.history()
        assert len(hist) == 3
        assert hist[0]["reversible"] is True
        assert hist[1]["reversible"] is False
        assert hist[2]["reversible"] is None

    def test_reversible_on_blocked_result(self):
        shield = WarderBox(budget=0.001)

        @shield.tool(cost=10.0, reversible=True)
        def expensive_tool(x: int) -> int:
            return x

        result = expensive_tool(1)
        assert result.blocked is True
        assert result.reversible is None

    def test_reversible_on_error_result(self):
        shield = WarderBox()

        @shield.tool(cost=0.01, reversible=False)
        def failing_tool(x: int) -> int:
            raise ValueError("boom")

        result = failing_tool(1)
        assert result.error is not None
        assert result.reversible is None

    def test_multiple_tools_independent_reversibility(self):
        shield = WarderBox()

        @shield.tool(cost=0.01, reversible=True)
        def tool_a() -> str:
            return "a"

        @shield.tool(cost=0.01, reversible=False)
        def tool_b() -> str:
            return "b"

        r1 = tool_a()
        r2 = tool_b()
        assert r1.reversible is True
        assert r2.reversible is False
