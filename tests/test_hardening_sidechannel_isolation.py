"""Self-audit (seventh pass): every observability side-channel must be isolated
from the tool call — a faulty estimator, tracer or logger cannot crash it."""

from __future__ import annotations

from warderbox import CostEstimator, Tracer, WarderBox


def test_j1_buggy_estimator_isolated():
    s = WarderBox(budget=5.0)

    class BadEstimator(CostEstimator):
        def record(self, *a, **k):
            raise RuntimeError("estimator boom")

    s._estimator = BadEstimator()

    @s.tool(cost=0.01)
    def t():
        return "ok"

    r = t()
    assert r.success is True and r.output == "ok"


def test_j2_buggy_tracer_isolated():
    s = WarderBox(budget=5.0)

    class BadTracer(Tracer):
        def start_span(self, *a, **k):
            raise RuntimeError("span boom")

        def end_span(self, *a, **k):
            raise RuntimeError("span boom")

    s.set_tracer(BadTracer())

    @s.tool(cost=0.01)
    def t():
        return "ok"

    assert t().success is True


def test_j3_buggy_logger_isolated():
    s = WarderBox(budget=5.0)

    class BadLogger:
        _masker = None

        def log(self, result):
            raise RuntimeError("log boom")

        def close(self):
            pass

    s._logger = BadLogger()

    @s.tool(cost=0.01)
    def t():
        return "ok"

    assert t().success is True
