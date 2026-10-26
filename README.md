<p align="center">
  <img src="https://raw.githubusercontent.com/alicelik-games/warderbox/main/assets/logo.png" alt="WarderBox Logo" width="180"/>
</p>

<h1 align="center">WarderBox</h1>

<p align="center">
  <strong>The safety layer between AI agents and the real world.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/warderbox/"><img src="https://img.shields.io/pypi/v/warderbox?color=blue" alt="PyPI"/></a>
  <a href="https://github.com/alicelik-games/warderbox/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20NC-blue" alt="License"/></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python"/></a>
  <a href="https://pypi.org/project/warderbox/"><img src="https://img.shields.io/pypi/dm/warderbox?color=orange" alt="Downloads"/></a>
</p>

WarderBox intercepts every tool call your AI agent makes and provides rollback, cost control, execution timeout, idempotency guard, output validation, fallback routing, execution quotas, smart model routing, cost anomaly detection, rate limiting, circuit breakers, approval workflows, distributed tracing, chaos testing, and compliance reporting — so agents can work autonomously without causing damage.

**Zero mandatory dependencies. One `pip install`. Works with every major agent framework.**

> **Market context:** With Guardrails AI acquired by Harvey (Sept 2026) and the AI agent safety market projected to grow from $610M to $6.85B at 41% CAGR, WarderBox fills the gap as a comprehensive, framework-agnostic agent operations platform.

## Why WarderBox?

| Problem | WarderBox Solution |
|---------|---------------------|
| Agent runs `DROP TABLE` | Auto-blocked by risk classification |
| Agent overspends API budget | Budget limits + cost forecasting |
| Want to test without side effects | Test mode simulates everything |
| Agent made a mistake | One-call rollback via state snapshots |
| Agent spams an API | Rate limiting (RPM/RPH per tool) |
| Tool keeps failing | Circuit breaker auto-disables it |
| Need human approval | Approval workflows with timeout/escalation |
| Different agents, different rules | AgentPool with isolated budgets + capabilities |
| Sensitive data in logs | Automatic PII/secret masking |
| Need audit trail | SQLite logging + Prometheus + compliance reports |
| Test agent resilience | Chaos engineering with fault injection |
| Track every action | OpenTelemetry-style distributed tracing |
| Evaluate agent quality | Safety/performance scoring (A-F grades) |
| LLM costs too high | Smart Model Routing picks cheapest capable model |
| Spending spikes unnoticed | Cost Anomaly Detection with z-score alerts |
| No cost visibility | Real-time FinOps Dashboard with 5 tabs |
| Tool call hangs forever | Execution Timeout stops waiting after the deadline |
| Agent retries double-fires | Idempotency Guard blocks duplicate calls |
| Tool leaks sensitive data | Output Validation redacts/blocks bad outputs |
| Circuit-broken tool blocks agent | Fallback Routing auto-switches to alternative |
| Agent exceeds monthly limit | Execution Quotas (daily/weekly/monthly caps) |

## Install

```bash
pip install warderbox
```

With framework integrations:

```bash
pip install warderbox[openai]      # OpenAI Agents SDK
pip install warderbox[anthropic]   # Anthropic Claude SDK
pip install warderbox[langchain]   # LangChain
pip install warderbox[crewai]      # CrewAI
pip install warderbox[autogen]     # Microsoft AutoGen
pip install warderbox[yaml]        # YAML policy files
pip install warderbox[all]         # Everything
```

## Quick Start

```python
from warderbox import WarderBox

shield = WarderBox(budget=5.00, auto_snapshot=True)

@shield.tool(cost=0.03)
def query_db(sql: str) -> str:
    return db.execute(sql)

# Normal execution
result = query_db(sql="SELECT * FROM users")
print(result.output)  # Works fine

# Destructive SQL is auto-blocked
result = query_db(sql="DROP TABLE users")
print(result.blocked)       # True
print(result.block_reason)  # "CRITICAL risk blocked: Destructive SQL detected"

# Undo the last action
shield.rollback()
```

## Core Features

### Policy-as-Code

Define rules in JSON or YAML files:

```json
{
  "budget": 25.00,
  "mode": "production",
  "global_rpm": 120,
  "tool_limits": {"send_email": {"rpm": 10, "rph": 100}},
  "rules": [
    {
      "name": "no_prod_access",
      "match": {"args_regex": "production|prod-db"},
      "risk": "critical",
      "reason": "Production database access blocked"
    }
  ],
  "webhooks": [
    {"url": "https://hooks.slack.com/...", "events": ["action_blocked"]}
  ]
}
```

```python
from warderbox import load_policy
shield = load_policy("policy.json")
```

### Multi-Agent Support

```python
from warderbox import AgentPool

pool = AgentPool(default_budget=5.00)
researcher = pool.register("researcher", budget=3.00)
coder = pool.register("coder", budget=10.00)
reviewer = pool.register("reviewer", budget=1.00, mode="test")

pool.stats()        # Aggregated view across all agents
pool.over_budget()  # ["researcher"] if over budget
```

### Rate Limiting

```python
from warderbox import WarderBox, ToolLimit

shield = WarderBox(
    budget=10.00,
    global_rpm=60,                                    # 60 calls/min globally
    tool_limits={"send_email": ToolLimit(rpm=5)},     # 5 emails/min
)
```

### Retry with Backoff

```python
from warderbox import RetryPolicy

policy = RetryPolicy(max_retries=3, backoff_base=1.0, jitter=0.5)

@shield.tool(cost=0.05, retry=policy)
def flaky_api(query: str) -> str:
    return requests.get(f"https://api.example.com?q={query}").text
```

### Sensitive Data Masking

```python
from warderbox import DataMasker

masker = DataMasker()
shield.set_masker(masker)

# Emails, API keys, credit cards, SSNs, JWTs are auto-masked in logs
masker.add_pattern("project_id", r"PROJ-\d{6}", "PROJ-******")
```

## Enterprise Safety

### Circuit Breaker

Auto-disable tools that fail repeatedly (CLOSED -> OPEN -> HALF_OPEN):

```python
from warderbox import CircuitBreaker

cb = CircuitBreaker(failure_threshold=5, reset_timeout=60.0)
shield.set_circuit_breaker(cb)
# After 5 consecutive failures, the tool is auto-disabled for 60s
# Then one probe call is allowed (HALF_OPEN) to test recovery
```

### Human-in-the-Loop Approval

```python
from warderbox import ApprovalManager

approver = ApprovalManager(default_timeout=30.0)
approver.require_approval("send_email", timeout=60.0)
approver.require_approval_for_risk("HIGH")
shield.set_approval(approver)

# From another thread or webhook handler:
approver.approve(request_id, approver="admin@company.com")
```

### Agent Capabilities / Permissions

Fine-grained tool access control per agent:

```python
from warderbox import WarderBox, CapabilityManager

caps = CapabilityManager()  # fail-closed by default: unknown agents are denied
caps.grant("researcher", allow=["search", "read_file"])
caps.grant("intern_bot", deny=["delete_record", "send_email"])
caps.add_constraint("coder", "run_sql", args_deny=["DROP", "TRUNCATE"])

# The shield must know which agent it represents so capabilities apply.
shield = WarderBox(budget=5.00, agent_name="researcher")
shield.set_capabilities(caps)
# (AgentPool.register sets agent_name automatically for each agent.)
# For a permissive setup, use CapabilityManager(fail_closed=False).
```

### Compliance & Audit Reports

```python
from warderbox import ComplianceReporter

reporter = ComplianceReporter(db_path="audit.db")
report = reporter.generate_report(start="2026-01-01", end="2026-12-31")
print(report.summary)          # Text summary with anomaly detection
reporter.export_json("audit.json")
html = reporter.to_html()      # Full HTML report
```

## Observability

### Distributed Tracing

OpenTelemetry-style span system (JSON export; not OTLP wire format):

```python
from warderbox import Tracer

tracer = Tracer(service_name="my-agent")
shield.set_tracer(tracer)

# Every tool call automatically generates spans
# Nested spans for complex workflows:
with tracer.span("data_pipeline") as parent:
    with tracer.span("extract", parent=parent):
        extract_data()
    with tracer.span("transform", parent=parent):
        transform_data()

tracer.export_json("traces.json")  # JSON export
tracer.export_otlp()               # OTLP-compatible dict
```

### Cost Forecasting

```python
from warderbox import CostForecaster

forecaster = CostForecaster(shield)
report = forecaster.forecast()
print(report.burn_rate_per_hour)       # $/hour
print(report.estimated_exhaustion)     # datetime
print(report.estimated_actions_remaining)
print(report.trend)                    # "increasing" / "stable" / "decreasing"
```

### Agent Scoring & Benchmarking

```python
from warderbox import AgentScorer, PoolScorer

scorer = AgentScorer(shield, agent_name="my-bot")
report = scorer.score()
print(report.overall_score)    # 0-100
print(report.grade)            # A/B/C/D/F
print(report.safety_score)     # Safety dimension
print(report.reliability_score)

# Rank all agents in a pool
pool_scorer = PoolScorer(pool)
print(pool_scorer.summary())   # Ranked leaderboard
```

### Web Dashboard

```bash
warderbox --db audit.db dashboard --port 8080
```

Real-time web UI with 5 tabs: Overview, FinOps, Estimation, Routing, and Alerts.

### Smart Model Routing (FinOps)

```python
from warderbox import ModelRouter

router = ModelRouter()
router.add_model("haiku", cost_per_1k=0.00025, max_complexity=0.35)
router.add_model("sonnet", cost_per_1k=0.003, max_complexity=0.65)
router.add_model("opus", cost_per_1k=0.015, max_complexity=1.0)

decision = router.route("Summarize this text")
print(decision.model)           # "haiku" (cheapest capable)
print(decision.estimated_cost_per_1k)  # $0.00025/1k tokens

stats = router.stats()
print(stats.savings_percent)    # 60-90% cost savings
```

### Cost Anomaly Detection

```python
from warderbox import AnomalyDetector

detector = AnomalyDetector(sensitivity=2.0, loop_threshold=10)

# Records costs and fires alerts on spikes
detector.record("search_api", 0.03)
detector.record("search_api", 5.00)  # spike! returns alerts

# Alert types: COST_SPIKE, RATE_SPIKE, BUDGET_BURN, LOOP_DETECTED
print(detector.stats())
```

### Prometheus Metrics

```python
from warderbox.metrics import start_metrics_server
start_metrics_server(shield, port=9090)
# Scrape http://localhost:9090/metrics
```

Exports: `warderbox_actions_total`, `warderbox_actions_blocked_total`, `warderbox_budget_spent_dollars`, per-tool and per-agent labels.

### Webhook Notifications

```python
shield.notifier.add_webhook(
    url="https://hooks.slack.com/services/xxx",
    events={"action_blocked", "budget_exceeded"},
)

shield.notifier.on("action_blocked", lambda event, data: print(f"Alert: {data}"))
```

## Resilience Testing

### Chaos Engineering

Test agent resilience with controlled fault injection:

```python
from warderbox import ChaosEngine, FaultType

chaos = ChaosEngine()
shield.set_chaos(chaos)

# Random failures (20% of calls)
chaos.add_fault("search_api", FaultType.ERROR, probability=0.2)

# Latency injection
chaos.add_fault("slow_api", FaultType.LATENCY, latency_ms=2000)

# Conditional faults
chaos.add_fault("db_query", FaultType.ERROR,
                condition=lambda args: "prod" in str(args))

# Wildcard — affect all tools
chaos.add_fault("*", FaultType.LATENCY, latency_ms=500, probability=0.1)
```

### Action Replay

```python
from warderbox.replay import ActionReplayer

replayer = ActionReplayer(db_path="audit.db", executors={"run_sql": my_fn})
report = replayer.replay_all(shield)
print(f"Match rate: {report.success_rate:.0%}")
```

### Execution Timeout

```python
from warderbox import WarderBox, TimeoutExecutor, TimeoutPolicy

shield = WarderBox(budget=5.00)
shield.set_timeout(TimeoutExecutor(TimeoutPolicy(
    default_seconds=30,
    tool_timeouts={"slow_api": 60, "fast_lookup": 5},
)))

@shield.tool(cost=0.05)
def slow_api(query: str) -> str:
    return external_api.call(query)  # times out after 60s
```

### Idempotency Guard

```python
from warderbox import WarderBox, IdempotencyGuard

shield = WarderBox(budget=5.00)
guard = IdempotencyGuard(default_ttl=3600)
guard.add_protected_tool("send_email")
guard.add_protected_tool("process_payment")
shield.set_idempotency(guard)

@shield.tool(cost=0.01)
def send_email(to: str, body: str) -> str:
    return smtp.send(to, body)  # retry won't double-send
```

### Output Validation

```python
from warderbox import WarderBox, OutputValidator, OutputRule, OutputAction

shield = WarderBox(budget=5.00)
validator = OutputValidator()
validator.add_pii_rules()  # SSN, credit card, API keys, emails
validator.add_rule(OutputRule(
    name="max_response_size", max_length=50000, action=OutputAction.BLOCK,
))
shield.set_output_validator(validator)
```

### Fallback Routing

```python
from warderbox import WarderBox, FallbackManager
from warderbox import CircuitBreaker

shield = WarderBox(budget=5.00)
shield.set_circuit_breaker(CircuitBreaker(failure_threshold=3))

fb = FallbackManager()
fb.register("gpt4_query", "gpt3_query",
    fallback_fn=gpt3_client.complete,
    fallback_cost=0.001)
shield.set_fallback(fb)
```

### Execution Quotas

```python
from warderbox import WarderBox, QuotaManager, QuotaPeriod

shield = WarderBox(budget=5.00)
qm = QuotaManager()
qm.set_quota("send_email", QuotaPeriod.DAILY, 100)
qm.set_quota("api_call", QuotaPeriod.MONTHLY, 10000)
qm.set_quota("global", QuotaPeriod.DAILY, 5000)
shield.set_quota(qm)
```

### Async Support

```python
from warderbox import AsyncWarderBox

shield = AsyncWarderBox(budget=5.00)

@shield.async_tool(cost=0.05)
async def async_query(sql: str) -> str:
    return await db.execute(sql)
```

## Framework Integrations

| Framework | Module | Pattern |
|-----------|--------|---------|
| OpenAI Agents SDK | `integrations.openai_agents` | Middleware |
| Anthropic Claude | `integrations.anthropic_sdk` | Middleware |
| LangChain | `integrations.langchain` | Tool wrapper |
| CrewAI | `integrations.crewai` | Tool wrapper |
| Microsoft AutoGen | `integrations.autogen` | Function wrapper |

```python
# OpenAI
from warderbox.integrations.openai_agents import OpenAIShieldMiddleware
middleware = OpenAIShieldMiddleware(shield=shield, executors={...})

# Anthropic
from warderbox.integrations.anthropic_sdk import AnthropicShieldMiddleware
middleware = AnthropicShieldMiddleware(shield=shield, executors={...})

# LangChain
from warderbox.integrations.langchain import shield_langchain_tools

# CrewAI
from warderbox.integrations.crewai import shield_crewai_tools

# AutoGen
from warderbox.integrations.autogen import shield_autogen_functions
```

## CLI

```bash
warderbox stats                    # Action statistics
warderbox logs                     # View action log
warderbox logs --blocked           # Only blocked actions
warderbox export audit.json        # Export to JSON
warderbox validate policy.json     # Validate a policy file
warderbox dashboard --port 8080    # Start web dashboard
```

## Risk Levels

| Level | Behavior | Examples |
|-------|----------|----------|
| LOW | Execute normally | SELECT queries, file reads |
| MEDIUM | Execute with logging | Outbound messages, moderate operations |
| HIGH | Ask for approval *(when an ApprovalManager or on_block callback is configured)* | Bulk operations (>100 items) |
| CRITICAL | Auto-block | DROP TABLE, TRUNCATE, DELETE FROM |

## Architecture

```
Agent -> WarderBox -> Tool
         |-- Idempotency Check (block duplicates)
         |-- Capability Check (permissions)
         |-- Circuit Breaker + Fallback Routing
         |-- Risk Classification (rules engine)
         |-- Approval Workflow (human-in-the-loop)
         |-- Budget Reserve (thread-safe)
         |-- Rate Limit Check
         |-- Quota Check (daily/weekly/monthly)
         |-- Snapshot (for rollback)
         |-- Chaos Injection (testing)
         |-- Execute with Timeout (or simulate)
         |-- Output Validation (redact/block)
         |-- Retry (on failure, with backoff)
         |-- Data Masking (for logs)
         |-- Log to SQLite
         |-- Emit webhook/callback events
         |-- Record span (distributed tracing)
         |-- Agent Scoring
         |-- Smart Model Routing (FinOps)
         |-- Cost Anomaly Detection
         |-- Prometheus metrics
```

## Stats

- **42 Python modules**, 493 tests
- **Zero mandatory dependencies**
- **5 framework integrations**
- Works with Python 3.11+

## Author

**Ali Celik** — [LinkedIn](https://linkedin.com/in/alicelik1980) · [Email](mailto:alicelik1980@gmail.com)

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

**Free for:** personal use, research, education, hobby projects, non-profit organizations, and government institutions.

**Commercial use requires a separate license.** If you want to use WarderBox in a commercial product, SaaS platform, or enterprise environment, please contact us for commercial licensing terms.

**Commercial Licensing Contact:** [alicelik1980@gmail.com](mailto:alicelik1980@gmail.com)
