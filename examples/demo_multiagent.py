"""Demo: Multi-agent setup with AgentPool.

Shows how to run multiple AI agents with isolated budgets,
rules, and monitoring — all managed from a single pool.
"""

import sys
sys.path.insert(0, "src")

from warderbox import WarderBox
from warderbox.multiagent import AgentPool
from warderbox.ratelimit import ToolLimit


def main():
    print("=" * 60)
    print("  WarderBox Multi-Agent Demo")
    print("=" * 60)

    # Create a pool with default settings
    pool = AgentPool(default_budget=5.00)

    # Register agents with different configs
    researcher = pool.register("researcher", budget=3.00)
    coder = pool.register("coder", budget=10.00)
    reviewer = pool.register("reviewer", budget=1.00, mode="test")

    # -- Researcher tools --
    @researcher.tool(cost=0.10)
    def web_search(query: str):
        return f"Found 5 results for: {query}"

    @researcher.tool(cost=0.05)
    def read_paper(url: str):
        return f"Paper summary from {url}"

    # -- Coder tools --
    @coder.tool(cost=0.02)
    def run_sql(query: str):
        return f"Query result: {query}"

    @coder.tool(cost=0.01)
    def write_file(path: str, content: str):
        return f"Wrote {len(content)} bytes to {path}"

    # -- Reviewer tools --
    @reviewer.tool(cost=0.03)
    def analyze_code(code: str):
        return f"Analysis: {len(code)} chars, looks good"

    # Simulate agent work
    print("\n--- Researcher working ---")
    r1 = web_search(query="AI agent safety best practices")
    print(f"  Search: {r1.output}")
    r2 = read_paper(url="https://arxiv.org/abs/2401.12345")
    print(f"  Paper: {r2.output}")

    print("\n--- Coder working ---")
    r3 = run_sql(query="SELECT * FROM tasks WHERE status='open'")
    print(f"  SQL: {r3.output}")
    r4 = write_file(path="/src/agent.py", content="class Agent: pass")
    print(f"  File: {r4.output}")
    r5 = run_sql(query="DROP TABLE users")
    print(f"  DROP TABLE blocked: {r5.blocked} — {r5.block_reason}")

    print("\n--- Reviewer working (test mode) ---")
    r6 = analyze_code(code="def hello(): return 'world'")
    print(f"  Analysis: {r6.output}")
    print(f"  (test mode = no real execution)")

    # Pool-level stats
    print("\n" + "-" * 60)
    print("  Pool Stats")
    print("-" * 60)
    stats = pool.stats()
    print(f"  Agents:        {stats['total_agents']}")
    print(f"  Total actions: {stats['total_actions']}")
    print(f"  Blocked:       {stats['total_blocked']}")
    print(f"  Total spent:   ${stats['total_spent']:.2f}")

    print("\n  Per-agent budgets:")
    for name, agent_stats in stats["per_agent"].items():
        b = agent_stats["budget"]
        spent = b["spent"]
        budget = b["budget"]
        print(f"    {name:12s}: ${spent:.2f} / ${budget:.2f}")

    # Unified history
    print(f"\n  Combined history ({len(pool.history())} entries):")
    for entry in pool.history():
        agent = entry.get("agent", "?")
        status = "BLOCKED" if entry["blocked"] else "OK"
        print(f"    [{agent:10s}] {entry['tool']:15s} {status}")

    print("\n" + "=" * 60)
    print("  Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
