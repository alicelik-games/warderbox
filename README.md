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

**Zero mandatory dependencies. One `pip install`. Works with 5 major agent frameworks — or plain Python.**

> **Market context:** With Guardrails AI acquired by Harvey (Sept 2026) and the AI agent safety market projected to grow from \$610M to \$6.85B at 41% CAGR, WarderBox fills the gap as a comprehensive, framework-agnostic agent operations platform.

## Why WarderBox?

| Problem | WarderBox Solution |
|---------|---------------------|
| Agent runs `DROP TABLE` | Auto-blocked by risk classification |
| Agent overspends API budget | Budget limits + cost forecasting |
| Want to test without side effects | Test mode simulates everything |
| Agent made a mistake | One-call rollback via state snapshots (tools with capture/restore) |
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
pip install warderbox[redis]       # Shared limits across machines
pip install warderbox[sql]         # SQL parser alongside the built-in detection
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

Timeouts are retried like any other error, and `retry_on` decides for them too.
When a thread-mode deadline passes the tool is still running, so a retry keeps
waiting for that same attempt and uses its result instead of running the tool
twice at once; a new attempt starts only once the first one has failed. For a
read-only tool that may hang, `RetryPolicy(overlap_on_timeout=True)` starts a new
attempt alongside it. Process isolation kills the old attempt, and coroutines are
cancelled, so their retries start fresh.

### Sensitive Data Masking

```python
from warderbox import DataMasker

# Masking is on by default for audit logs and webhooks. Attach a custom
# masker if you want additional patterns:
masker = DataMasker()
shield.set_masker(masker)
masker.add_pattern("project_id", r"PROJ-\d{6}", "PROJ-******")

# Opt out explicitly if raw logs are required:
# shield.set_masker(DataMasker(enabled=False))
```

Masking covers strings, bytes, tuples, sets, dict keys and objects (through
`str()`), plus common credential formats: OpenAI, Anthropic, Stripe, GitHub,
GitLab, Slack, Google, Hugging Face, npm, SendGrid, AWS, PEM private keys and
passwords in URLs. Everything under a sensitive key (`password`, `api_key`,
`token`, ...) is redacted however deeply nested. It is pattern based: a secret
in an unknown format is only caught under a sensitive key.

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
approver.require_approval_for_risk("HIGH")  # applies to HIGH and CRITICAL
# auto_deny=False surfaces a timeout as ESCALATED (manual review), not a silent deny
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

Reports are evidence you can use in SOC 2, GDPR or internal audit work.
Generating them does not make a system compliant or certified.

### Audit Log Integrity

Every action is written to a hash chain. What verification proves depends on
how you run it:

| Mode | Detects | Does not detect |
|------|---------|-----------------|
| No key | Edited, reordered, removed or truncated rows | Anyone with write access recomputing the whole chain |
| `audit_key=` or `WARDERBOX_AUDIT_KEY` | Also: forged entries and a rewritten chain | Restoring an old copy of the file; deleting the whole file |
| Key + external anchor | Also: rollback or deletion past the anchor | Entries written after the last anchor, if the file is rolled back to it |


Keyless logs stay the backwards-compatible default. For production, opt in to
the stronger mode with `require_audit_key=True` (or `require_audit_key: true` in
a policy); then `WarderBox` refuses to start without `audit_key=` or
`WARDERBOX_AUDIT_KEY`.

In a policy file, name the environment variable that holds the key rather than
writing the key itself: whoever can read the file could otherwise sign entries.
A named variable that is not set stops startup instead of falling back to an
unkeyed log.

```yaml
db_path: audit.db
audit_key_env: WARDERBOX_AUDIT_KEY
require_audit_key: true
```

Opening a keyed log with a different key raises `AuditKeyError` before anything
is written, so a mistyped key cannot damage the log. Without a key a keyed log
can be read (the CLI dashboard does this) but not written to.

```python
shield = WarderBox(budget=5.0, db_path="audit.db", audit_key=os.environ["AUDIT_KEY"])

anchor = shield.audit_head()      # store this where the agent cannot write
shield.verify_audit()             # chain + state
shield.verify_audit(anchor)       # also proves nothing before the anchor was lost
```

Moving an existing database to a key is explicit: call
`shield.adopt_unkeyed_audit_history()` once, after checking the log. Until
then verification fails, so a forged unkeyed log cannot be adopted silently.
If logging itself fails, the tool call still returns, the `audit_log_error`
event fires and `stats()["audit_log_errors"]` goes up.

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

# For provider-reported charges, reconcile the declared estimate:
@shield.tool(cost=0.03, actual_cost=lambda output: 0.017)
def llm_call(prompt: str):
    ...

# or after the fact:
shield.reconcile_cost(action_id, actual_cost=0.017)
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

Delivery resolves the hostname on every attempt, requires every address to be
public and connects to those exact addresses, so a DNS answer cannot change
between the check and the connection. Redirects are not followed.

`HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are honored with the same checks: the
proxy is asked for the validated address, and only if it refuses that (a proxy
with a hostname allowlist) for the hostname. Where the local network has no
external DNS, the proxy is asked by hostname and does the resolving itself, so
that one case relies on the proxy to keep internal hosts out of reach.
`allow_private_webhooks=True` allows private targets.

## Resilience Testing

### Chaos Engineering

Test agent resilience with controlled fault injection:

```python
from warderbox import ChaosEngine, FaultType

# Chaos is inert in production by default; opt in explicitly for a drill.
# With allow_production=True, faults fire against real calls (and a RuntimeWarning
# is raised when attached). In test mode they fire without running the real tool.
chaos = ChaosEngine(allow_production=True)
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

# Also available: FaultType.TIMEOUT and FaultType.CORRUPT_OUTPUT
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

# By default a timeout stops waiting but the tool KEEPS RUNNING in its thread,
# and its side effect may still land. Automatic retries are disabled after a
# timeout so that side effect cannot be duplicated.
```

To actually stop the work, use process isolation for everything or per tool:

```python
TimeoutPolicy(default_seconds=30, isolation="process")
TimeoutPolicy(default_seconds=30, process_tools={"run_migration"})
```

The tool then runs in a forked child that is terminated at the deadline. That
requires `fork` (not Windows) and a picklable return value. In-memory changes
the tool makes stay in the child, and anything it already sent to an external
system cannot be recalled.

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

SQL detection reads comments, quoting and multiple statements the way databases
do, and with `warderbox[sql]` adds a real parser. It looks for statements in any
argument, so it cannot tell every free-text field from SQL: outside SQL fields
only text that reads as a statement counts. It is a safety net against agent mistakes, not a
security boundary. Give the agent's database user only the permissions it needs.

## Architecture

```
Agent -> WarderBox -> Tool
         |-- Capability Check (permissions)
         |-- Idempotency Check (block duplicates)
         |-- Circuit Breaker + Fallback Routing
         |-- Risk Classification (rules engine)
         |-- Approval Workflow (human-in-the-loop)
         |-- Your custom guards
         |-- Budget Reserve
         |-- Rate Limit Reserve
         |-- Quota Reserve (daily/weekly/monthly)
         |-- Idempotency Slot
         |-- Snapshot (for rollback)
         |-- Test Mode (simulate and stop here)
         |-- Chaos Injection (test mode, or with opt-in)
         |-- Execute with Timeout and Retry
         |-- Output Validation (redact/block)
         |-- Actual Cost, Settle Reservations
         |-- Data Masking (for logs)
         |-- Log to SQLite (hash chain)
         |-- Emit webhook/callback events
         |-- Record span (distributed tracing)
         |-- Cost Anomaly Detection
```

A fallback goes through these same guards, so a gate cannot exist on one path
and be missing on the other. Each guard that reserved something releases it, in
reverse order, if a later guard stops the call or the call is interrupted.

### Custom Guards

```python
from warderbox import Block, Guard

class BusinessHours(Guard):
    name = "business_hours"

    def check(self, ctx):
        if not is_business_hours():
            return Block(f"{ctx.tool} is only allowed in business hours")

shield.add_guard(BusinessHours())  # runs before budget is reserved
```

Override `check`, `reserve`, `release` and `commit` as needed. A guard that
raises stops the call rather than letting it through.

### Shared limits across workers

By default budget, rate-limit and quota counters live in process memory: four
gunicorn workers with a \$5 budget can spend \$20. Give them a backend to share:

```python
from warderbox import RedisStateBackend, SQLiteStateBackend, WarderBox

# Workers on one machine, no dependencies
backend = SQLiteStateBackend("warderbox-state.db")
# Workers across machines: pip install warderbox[redis]
backend = RedisStateBackend("redis://localhost:6379/0")

shield = WarderBox(budget=5.0, state_backend=backend, namespace="prod")
shield.set_quota(quota)  # shares the same backend
```

Or in a policy file: `{"namespace": "prod", "state": {"backend": "sqlite", "path": "state.db"}}`.

- Each check-and-reserve is atomic across workers.
- A crashed worker's budget reservation returns after its time-to-live (1 hour
  by default, `BudgetTracker(reservation_ttl=)`). Make it longer than your
  slowest tool.
- If the backend is unreachable new calls are **blocked**, not run unmetered,
  and `state_backend_error` fires. A charge that could not be written is kept
  and retried, and shows in `budget_summary["pending_charges"]`.
- Cost entries, circuit breakers, the estimator and anomaly detection remain per
  process. Idempotency has its own `RedisBackend`.

### Real cost vs declared cost

`cost=` is your estimate. Unless a tool reports its real charge, budgets and
forecasts are projecting that estimate:

```python
from warderbox import cost_from_usage

@shield.tool(cost=0.05, actual_cost=cost_from_usage(3.0, 15.0))  # $ per MTok
def call_model(prompt):
    return client.messages.create(...)  # output has a .usage report

shield.set_actual_cost("search", lambda out: out["billed"])  # for adapter tools
shield.reconcile_cost(action_id, 0.42)                       # after the invoice
```

Results and `history()` carry `cost_source` (`declared`, `actual` or
`reconciled`), and forecasts report `actual_cost_share`. `reconcile_cost` updates
the budget, history, estimator and anomaly detector. The audit log is append-only
and keeps the cost known at call time; the `cost_reconciled` event records the
change.

### Rollback scope

`rollback()` restores state captured by the tool's `snapshot_state` /
`restore_state` callbacks. It cannot undo an email already sent, a payment
captured, or a remote API write unless the tool provides a compensating
restore function.

## Stats

- **45 Python modules**, 1013 tests
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
