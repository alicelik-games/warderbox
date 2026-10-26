"""Tests for Phase 5: multi-agent, AutoGen, Prometheus metrics."""

import sys
import urllib.request

sys.path.insert(0, "src")

from warderbox import AgentPool, WarderBox
from warderbox.integrations.autogen import AutoGenShieldProxy, shield_autogen_functions
from warderbox.metrics import multiagent_metrics, prometheus_metrics, start_metrics_server
from warderbox.multiagent import AgentPool

# -- Multi-agent --

def test_agent_pool_basic():
    pool = AgentPool(default_budget=5.00)

    s1 = pool.register("agent_a", budget=3.00)
    s2 = pool.register("agent_b", budget=10.00)

    assert "agent_a" in pool
    assert "agent_b" in pool
    assert pool.agents == ["agent_a", "agent_b"]

    @s1.tool(cost=0.10)
    def search(q: str):
        return f"found: {q}"

    @s2.tool(cost=0.05)
    def code(task: str):
        return f"coded: {task}"

    search(q="test")
    code(task="write function")
    code(task="DROP TABLE x")

    stats = pool.stats()
    assert stats["total_agents"] == 2
    assert stats["total_actions"] == 3
    assert stats["total_blocked"] == 1


def test_agent_pool_isolation():
    pool = AgentPool()

    s1 = pool.register("spender", budget=0.10)
    s2 = pool.register("saver", budget=100.00)

    @s1.tool(cost=0.05)
    def expensive(x: str):
        return x

    @s2.tool(cost=0.01)
    def cheap(x: str):
        return x

    expensive(x="a")
    expensive(x="b")
    r3 = expensive(x="c")
    assert r3.blocked

    r_saver = cheap(x="still fine")
    assert not r_saver.blocked

    assert pool.over_budget() == ["spender"]


def test_agent_pool_history():
    pool = AgentPool()
    s1 = pool.register("a")
    s2 = pool.register("b")

    @s1.tool(cost=0.01)
    def tool_a(x: str):
        return x

    @s2.tool(cost=0.01)
    def tool_b(x: str):
        return x

    tool_a(x="1")
    tool_b(x="2")
    tool_a(x="3")

    all_history = pool.history()
    assert len(all_history) == 3
    agents = [h["agent"] for h in all_history]
    assert "a" in agents
    assert "b" in agents

    a_history = pool.history("a")
    assert len(a_history) == 2


def test_agent_pool_duplicate_registration():
    pool = AgentPool()
    pool.register("x")
    try:
        pool.register("x")
        assert False, "Should raise ValueError"
    except ValueError:
        pass


# -- AutoGen --

def test_autogen_shield_functions():
    shield = WarderBox(budget=5.0)

    def search(query: str) -> str:
        return f"results: {query}"

    def danger(query: str) -> str:
        return f"executed: {query}"

    wrapped = shield_autogen_functions(
        shield=shield,
        functions={"search": search, "danger": danger},
        cost_map={"search": 0.05},
    )

    r1 = wrapped["search"](query="AI safety")
    assert "results: AI safety" in r1

    r2 = wrapped["danger"](query="DROP TABLE users")
    assert "BLOCKED" in r2


def test_autogen_proxy():
    shield = WarderBox(budget=5.0)

    proxy = AutoGenShieldProxy(shield, cost_map={"calc": 0.01})

    original_map = {
        "calc": lambda expression: f"= {eval(expression)}",
    }

    wrapped = proxy.wrap(original_map)
    result = wrapped["calc"](expression="2 + 2")
    assert "= 4" in result


# -- Prometheus Metrics --

def test_prometheus_metrics_format():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.05)
    def query(sql: str):
        return "ok"

    query(sql="SELECT 1")
    query(sql="DROP TABLE x")
    query(sql="SELECT 2")

    text = prometheus_metrics(shield)

    assert "warderbox_actions_total 3" in text
    assert "warderbox_actions_blocked_total 1" in text
    assert "warderbox_actions_successful_total 2" in text
    assert "warderbox_budget_spent_dollars" in text
    assert 'warderbox_tool_calls_last_minute{tool="query"}' in text
    assert "# TYPE" in text
    assert "# HELP" in text


def test_prometheus_metrics_server():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.01)
    def ping(msg: str):
        return "pong"

    ping(msg="test")

    server = start_metrics_server(shield, port=0, background=True)
    port = server.server_address[1]

    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics")
        text = resp.read().decode()
        assert "warderbox_actions_total 1" in text
        assert resp.headers["Content-Type"].startswith("text/plain")
    finally:
        server.shutdown()


def test_multiagent_metrics():
    pool = AgentPool()
    s1 = pool.register("agent1", budget=5.0)
    s2 = pool.register("agent2", budget=3.0)

    @s1.tool(cost=0.1)
    def tool1(x: str):
        return x

    @s2.tool(cost=0.05)
    def tool2(x: str):
        return x

    tool1(x="a")
    tool2(x="b")

    text = multiagent_metrics(pool)
    assert "warderbox_pool_agents_total 2" in text
    assert "warderbox_pool_actions_total 2" in text
    assert 'agent="agent1"' in text
    assert 'agent="agent2"' in text


if __name__ == "__main__":
    test_agent_pool_basic()
    test_agent_pool_isolation()
    test_agent_pool_history()
    test_agent_pool_duplicate_registration()
    test_autogen_shield_functions()
    test_autogen_proxy()
    test_prometheus_metrics_format()
    test_prometheus_metrics_server()
    test_multiagent_metrics()
    print("All Phase 5 tests passed!")
