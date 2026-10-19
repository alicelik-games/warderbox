"""Routing benchmark — compares cost and quality with vs without smart routing.

Simulates a real workload of mixed tool calls where some tools fail or are
expensive, and measures how fallback routing affects total cost and success rate.

Usage:
    python benchmarks/routing_benchmark.py
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from warderbox import (
    WarderBox,
    FallbackManager,
    FallbackRoute,
)
from warderbox.circuitbreaker import CircuitBreaker


@dataclass
class BenchmarkResult:
    label: str
    total_calls: int = 0
    successful: int = 0
    blocked: int = 0
    errors: int = 0
    total_cost: float = 0.0
    duration_ms: float = 0.0
    fallbacks_used: int = 0

    @property
    def success_rate(self) -> float:
        return (self.successful / self.total_calls * 100) if self.total_calls else 0.0

    @property
    def avg_cost(self) -> float:
        return (self.total_cost / self.successful) if self.successful else 0.0


TASK_SET = [
    ("gpt4_query", 0.03),
    ("gpt4_query", 0.03),
    ("gpt4_query", 0.03),
    ("web_search", 0.01),
    ("web_search", 0.01),
    ("code_exec", 0.005),
    ("code_exec", 0.005),
    ("gpt4_query", 0.03),
    ("image_gen", 0.05),
    ("web_search", 0.01),
    ("gpt4_query", 0.03),
    ("code_exec", 0.005),
    ("image_gen", 0.05),
    ("gpt4_query", 0.03),
    ("web_search", 0.01),
    ("gpt4_query", 0.03),
    ("code_exec", 0.005),
    ("gpt4_query", 0.03),
    ("image_gen", 0.05),
    ("web_search", 0.01),
    ("gpt4_query", 0.03),
    ("gpt4_query", 0.03),
    ("code_exec", 0.005),
    ("web_search", 0.01),
    ("gpt4_query", 0.03),
    ("image_gen", 0.05),
    ("web_search", 0.01),
    ("code_exec", 0.005),
    ("gpt4_query", 0.03),
    ("web_search", 0.01),
]

FAILURE_RATES = {
    "gpt4_query": 0.40,
    "web_search": 0.20,
    "code_exec": 0.15,
    "image_gen": 0.50,
}

FALLBACK_MAP = {
    "gpt4_query": ("claude_query", 0.02),
    "web_search": ("cached_search", 0.002),
    "code_exec": ("sandbox_exec", 0.003),
    "image_gen": ("image_gen_v2", 0.04),
}

call_counter = 0


def make_flaky_fn(tool_name: str, failure_rate: float):
    def fn(**kwargs):
        global call_counter
        call_counter += 1
        if random.random() < failure_rate:
            raise ConnectionError(f"{tool_name} unavailable")
        return f"{tool_name}_result_{call_counter}"
    fn.__name__ = tool_name
    fn.__qualname__ = tool_name
    return fn


def make_fallback_fn(fallback_name: str):
    def fn(**kwargs):
        global call_counter
        call_counter += 1
        return f"{fallback_name}_result_{call_counter}"
    fn.__name__ = fallback_name
    fn.__qualname__ = fallback_name
    return fn


def run_without_routing(seed: int) -> BenchmarkResult:
    random.seed(seed)
    global call_counter
    call_counter = 0

    shield = WarderBox(budget=10.0)
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=9999)
    shield.set_circuit_breaker(cb)

    tools = {}
    for name, cost in {
        "gpt4_query": 0.03, "web_search": 0.01,
        "code_exec": 0.005, "image_gen": 0.05,
    }.items():
        fn = make_flaky_fn(name, FAILURE_RATES[name])
        tools[name] = shield.tool(cost=cost)(fn)

    result = BenchmarkResult(label="Without Routing")
    t0 = time.monotonic()

    for task_name, _cost in TASK_SET:
        r = tools[task_name](query=f"task_{result.total_calls}")
        result.total_calls += 1
        if r.blocked:
            result.blocked += 1
        elif r.error:
            result.errors += 1
        else:
            result.successful += 1
            result.total_cost += r.cost

    result.duration_ms = (time.monotonic() - t0) * 1000
    return result


def run_with_routing(seed: int) -> BenchmarkResult:
    random.seed(seed)
    global call_counter
    call_counter = 0

    shield = WarderBox(budget=10.0)
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=9999)
    shield.set_circuit_breaker(cb)

    fm = FallbackManager()
    shield.set_fallback(fm)

    tools = {}
    for name, cost in {
        "gpt4_query": 0.03, "web_search": 0.01,
        "code_exec": 0.005, "image_gen": 0.05,
    }.items():
        fn = make_flaky_fn(name, FAILURE_RATES[name])
        tools[name] = shield.tool(cost=cost)(fn)

    for primary, (fallback_name, fallback_cost) in FALLBACK_MAP.items():
        fm.register(
            primary_tool=primary,
            fallback_tool=fallback_name,
            fallback_fn=make_fallback_fn(fallback_name),
            fallback_cost=fallback_cost,
        )

    result = BenchmarkResult(label="With Smart Routing")
    t0 = time.monotonic()

    for task_name, _cost in TASK_SET:
        r = tools[task_name](query=f"task_{result.total_calls}")
        result.total_calls += 1
        if r.blocked:
            result.blocked += 1
        elif r.error:
            result.errors += 1
        else:
            result.successful += 1
            result.total_cost += r.cost

    result.duration_ms = (time.monotonic() - t0) * 1000
    result.fallbacks_used = sum(
        fm._fallback_counts.get(k, 0) for k in FALLBACK_MAP
    )
    return result


def print_comparison(without: BenchmarkResult, with_rt: BenchmarkResult):
    efficiency_saved = without.avg_cost - with_rt.avg_cost
    eff_pct = (efficiency_saved / without.avg_cost * 100) if without.avg_cost else 0

    print("=" * 64)
    print("  WARDERBOX ROUTING BENCHMARK")
    print("=" * 64)
    print(f"  Task set: {len(TASK_SET)} calls across 4 tool types")
    print(f"  Failure rates: GPT-4 40%, Search 20%, Code 15%, Image 50%")
    print(f"  Circuit breaker: opens after 2 consecutive failures")
    print()

    row = "{:<25} {:>15} {:>15}"
    print(row.format("Metric", "No Routing", "Smart Routing"))
    print("-" * 64)
    print(row.format("Total calls", str(without.total_calls), str(with_rt.total_calls)))
    print(row.format("Successful", str(without.successful), str(with_rt.successful)))
    print(row.format("Errors", str(without.errors), str(with_rt.errors)))
    print(row.format("Blocked (circuit)", str(without.blocked), str(with_rt.blocked)))
    print(row.format("Fallbacks used", "—", str(with_rt.fallbacks_used)))
    print(row.format("Success rate", f"{without.success_rate:.1f}%", f"{with_rt.success_rate:.1f}%"))
    print(row.format("Total cost", f"${without.total_cost:.4f}", f"${with_rt.total_cost:.4f}"))
    print(row.format("Avg cost/success", f"${without.avg_cost:.4f}", f"${with_rt.avg_cost:.4f}"))
    print(row.format("Duration", f"{without.duration_ms:.1f}ms", f"{with_rt.duration_ms:.1f}ms"))
    print("-" * 64)

    success_gain = with_rt.successful - without.successful
    success_pct = (success_gain / without.successful * 100) if without.successful else 0

    print(f"\n  Results:")
    print(f"    Success rate:     {without.success_rate:.1f}% -> {with_rt.success_rate:.1f}% (+{success_pct:.0f}% improvement)")
    print(f"    Blocked calls:    {without.blocked} -> {with_rt.blocked} (eliminated by fallback)")
    print(f"    Cost/success:     ${without.avg_cost:.4f} -> ${with_rt.avg_cost:.4f} ({eff_pct:+.1f}%)")
    print(f"    Fallbacks used:   {with_rt.fallbacks_used} (cheaper alternatives)")
    print()


def main():
    n_runs = 10
    all_without = []
    all_with = []

    for i in range(n_runs):
        all_without.append(run_without_routing(seed=42 + i))
        all_with.append(run_with_routing(seed=42 + i))

    avg_without = BenchmarkResult(label="Without Routing (avg)")
    avg_with = BenchmarkResult(label="With Smart Routing (avg)")

    for r in all_without:
        avg_without.total_calls += r.total_calls
        avg_without.successful += r.successful
        avg_without.errors += r.errors
        avg_without.blocked += r.blocked
        avg_without.total_cost += r.total_cost
        avg_without.duration_ms += r.duration_ms

    for r in all_with:
        avg_with.total_calls += r.total_calls
        avg_with.successful += r.successful
        avg_with.errors += r.errors
        avg_with.blocked += r.blocked
        avg_with.total_cost += r.total_cost
        avg_with.duration_ms += r.duration_ms
        avg_with.fallbacks_used += r.fallbacks_used

    for avg in (avg_without, avg_with):
        avg.total_calls //= n_runs
        avg.successful //= n_runs
        avg.errors //= n_runs
        avg.blocked //= n_runs
        avg.total_cost /= n_runs
        avg.duration_ms /= n_runs
    avg_with.fallbacks_used //= n_runs

    print_comparison(avg_without, avg_with)

    print(f"  Averaged over {n_runs} runs (seeds 42–{42 + n_runs - 1})")
    print()


if __name__ == "__main__":
    main()
