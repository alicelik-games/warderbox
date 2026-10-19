"""WarderBox — Full End-to-End Demo

Demonstrates every major feature in a single script:
- Risk classification & auto-blocking
- Budget control & cost tracking
- Rollback via state snapshots
- Circuit breaker
- Agent capabilities
- Distributed tracing
- Chaos engineering
- Cost forecasting
- Agent scoring
- Multi-agent pool with rankings
- Data masking
- Compliance reporting

Run: python3 examples/demo_full.py
"""

import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from warderbox import (
    WarderBox, AgentPool, AgentScorer, PoolScorer,
    CapabilityManager, CircuitBreaker, ChaosEngine, FaultType,
    CostForecaster, ComplianceReporter, DataMasker, Tracer,
)


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


# Simulated database
db_data = {"users": ["alice", "bob", "charlie"]}


def main():
    # Temp DB for logging
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = db_file.name
    db_file.close()

    # =========================================================
    separator("1. Basic Shield: Risk Classification & Budget")
    # =========================================================

    shield = WarderBox(budget=5.00, auto_snapshot=True, db_path=db_path)

    @shield.tool(cost=0.05, snapshot_state=lambda: dict(db_data), restore_state=lambda s: db_data.update(s))
    def query_db(sql: str) -> str:
        if sql.upper().startswith("SELECT"):
            return f"Results: {db_data.get('users', [])}"
        return f"Executed: {sql}"

    r1 = query_db(sql="SELECT * FROM users")
    print(f"Safe query: {r1.output}")

    r2 = query_db(sql="DROP TABLE users")
    print(f"Destructive query blocked: {r2.blocked} — {r2.block_reason}")

    r3 = query_db(sql="SELECT name FROM users LIMIT 10")
    print(f"Another safe query: {r3.output}")

    print(f"\nBudget: ${shield._budget.spent:.2f} / ${shield._budget.budget:.2f}")

    # =========================================================
    separator("2. Rollback")
    # =========================================================

    print(f"DB before: {db_data['users']}")
    db_data["users"].append("dave")
    r4 = query_db(sql="SELECT after insert")
    print(f"DB after insert: {db_data['users']}")

    shield.rollback()
    print(f"DB after rollback: {db_data['users']}")

    # =========================================================
    separator("3. Circuit Breaker")
    # =========================================================

    cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
    shield.set_circuit_breaker(cb)

    @shield.tool(cost=0.01)
    def flaky_api(q: str):
        raise ConnectionError("Service unavailable")

    for i in range(4):
        r = flaky_api(q=f"query-{i}")
        state = cb.get_state("flaky_api").value
        print(f"  Call {i+1}: error={bool(r.error)}, blocked={r.blocked}, circuit={state}")

    print(f"\nCircuit breaker stats: {cb.stats()}")

    # =========================================================
    separator("4. Agent Capabilities")
    # =========================================================

    caps = CapabilityManager()
    caps.grant("intern", allow=["query_db"], deny=["flaky_api"])
    caps.add_constraint("intern", "query_db", args_deny=["DROP", "DELETE"])
    shield.set_capabilities(caps)
    shield._agent_name = "intern"

    r_ok = query_db(sql="SELECT 1")
    print(f"Intern SELECT: blocked={r_ok.blocked}")

    r_block = query_db(sql="DELETE FROM users WHERE 1=1")
    print(f"Intern DELETE: blocked={r_block.blocked} — {r_block.block_reason}")

    shield._agent_name = ""
    shield._capabilities = None

    # =========================================================
    separator("5. Data Masking")
    # =========================================================

    masker = DataMasker()
    test_data = {
        "email": "agent@openai.com",
        "key": "sk-abc123def456ghi789jkl012mno",
        "ssn": "123-45-6789",
        "query": "SELECT * FROM users",
    }
    masked = masker.mask(test_data)
    print(f"Original: {test_data}")
    print(f"Masked:   {masked}")

    detected = masker.detect("Contact user@example.com with key sk-test1234567890abcdef")
    print(f"Detected patterns: {detected}")

    # =========================================================
    separator("6. Distributed Tracing")
    # =========================================================

    shield._circuit_breaker = None
    tracer = Tracer(service_name="demo-agent")
    shield.set_tracer(tracer)

    with tracer.span("demo_workflow") as parent:
        with tracer.span("phase_1", parent=parent):
            query_db(sql="SELECT count(*) FROM users")
        with tracer.span("phase_2", parent=parent):
            query_db(sql="SELECT name FROM users")

    print(f"Total spans: {tracer.stats()['total_spans']}")
    print(f"Traces: {tracer.stats()['traces']}")
    for s in tracer.spans()[:5]:
        print(f"  [{s.status}] {s.name} ({s.duration_ms:.1f}ms)")

    # =========================================================
    separator("7. Chaos Engineering")
    # =========================================================

    shield._tracer = None
    chaos = ChaosEngine(seed=42)
    chaos.add_fault("query_db", FaultType.ERROR,
                    probability=0.5, error_message="Chaos: random failure!")
    shield.set_chaos(chaos)

    successes = errors = 0
    for i in range(10):
        r = query_db(sql=f"SELECT {i}")
        if r.error:
            errors += 1
        else:
            successes += 1

    print(f"10 calls with 50% fault injection:")
    print(f"  Successes: {successes}, Errors: {errors}")
    print(f"  Chaos stats: {chaos.stats()}")

    shield._chaos = None

    # =========================================================
    separator("8. Cost Forecasting")
    # =========================================================

    forecaster = CostForecaster(shield)
    forecast = forecaster.forecast()
    print(forecast.summary)

    # =========================================================
    separator("9. Agent Scoring")
    # =========================================================

    scorer = AgentScorer(shield, agent_name="demo-agent")
    report = scorer.score()
    print(report.summary)

    # =========================================================
    separator("10. Multi-Agent Pool + Rankings")
    # =========================================================

    pool = AgentPool(default_budget=10.0)
    alpha = pool.register("alpha", budget=10.0)
    beta = pool.register("beta", budget=10.0)

    @alpha.tool(cost=0.02)
    def alpha_search(q: str):
        return f"alpha: {q}"

    @beta.tool(cost=0.02)
    def beta_search(q: str):
        return f"beta: {q}"

    @beta.tool(cost=0.01)
    def beta_risky(sql: str):
        return sql

    for i in range(5):
        alpha_search(q=f"query-{i}")
    for i in range(3):
        beta_search(q=f"query-{i}")
    beta_risky(sql="DROP TABLE important")
    beta_risky(sql="DELETE FROM logs")

    pool_scorer = PoolScorer(pool)
    print(pool_scorer.summary())
    print(f"\nPool stats: {pool.stats()['total_actions']} total actions")

    # =========================================================
    separator("11. Compliance Report")
    # =========================================================

    shield._logger.close()
    reporter = ComplianceReporter(db_path)
    report = reporter.generate_report()
    print(report.summary)

    # Cleanup
    os.unlink(db_path)

    # =========================================================
    separator("DONE")
    # =========================================================

    print("WarderBox full demo completed successfully!")
    print(f"\nFeatures demonstrated:")
    print(f"  - Risk classification & auto-blocking")
    print(f"  - Budget control & cost tracking")
    print(f"  - Rollback via state snapshots")
    print(f"  - Circuit breaker (CLOSED/OPEN/HALF_OPEN)")
    print(f"  - Agent capabilities & permissions")
    print(f"  - PII/secret data masking")
    print(f"  - Distributed tracing (OTel-compatible)")
    print(f"  - Chaos engineering (fault injection)")
    print(f"  - Cost forecasting & burn rate")
    print(f"  - Agent scoring & grading (A-F)")
    print(f"  - Multi-agent pool rankings")
    print(f"  - Compliance audit reports")


if __name__ == "__main__":
    main()
