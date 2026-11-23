# Changelog

All notable changes to WarderBox are documented here.

## [0.3.8] - 2026-11-24

Packaging and documentation only; no code changes from 0.3.7.

### Fixed
- **License metadata** (`pyproject.toml`): PyPI now shows the PolyForm Noncommercial 1.0.0 license (SPDX `LicenseRef-PolyForm-Noncommercial-1.0.0`) instead of "Other/Proprietary License"
- **README**: the Chaos Engineering example's comment was cut off mid-sentence

## [0.3.7] - 2026-11-07

Security, correctness and architecture work after 0.3.6 (1013 tests). **0.3.6 on
PyPI can downgrade a CRITICAL action to a lower risk when several rules match
(`DROP TABLE users; UPDATE users SET a=1` runs). Upgrade.**

### Added
- **Guard pipeline** (`guards.py`, `shield.py`, `async_shield.py`): primary and fallback calls, sync and async, run one list of guards. Replaces a 338-line copy of the pipeline in `_run_fallback`. Guards release what they hold in reverse order on a block or interruption. `shield.add_guard()` adds your own
- **Shared state** (`state.py`): `SQLiteStateBackend` and `RedisStateBackend` make budget, rate and quota limits hold across workers, with atomic reservations, a time-to-live on budget holds, fail-closed behavior during an outage and retried charges. Set with `WarderBox(state_backend=, namespace=)` or a policy's `state` section
- **Keyed audit log** (`logger.py`): `audit_key=` / `WARDERBOX_AUDIT_KEY` (HMAC entries), `audit_head()` + `verify_integrity(expected_head=)` for external anchoring, `verify_report()` with a reason, `adopt_unkeyed_history()`. Key enforcement is opt-in (`require_audit_key=True`), and `AgentPool` / policy files can pass `audit_key` or `audit_key_env`
- **Process isolation for timeouts** (`timeout.py`): `TimeoutPolicy(isolation="process")` or `process_tools={...}` terminates the worker at the deadline
- **Real cost tracking** (`budget.py`, `shield.py`, `forecast.py`): `cost_from_usage()`, `shield.set_actual_cost()`, `cost_source` on results and history, `actual_cost_share` on forecasts
- **Events**: `audit_log_error`, `state_backend_error`, `cost_reconciled`, `guard_error`
- **Extras**: `warderbox[redis]`, `warderbox[sql]`

### Fixed
- **Risk escalation** (`rules.py`): removed the lexicographic string comparison that could downgrade CRITICAL to HIGH/MEDIUM when multiple rules matched; risk now always uses the enum order
- **Fallback crash** (`shield.py`): a fallback whose output failed validation raised `UnboundLocalError` instead of returning a result
- **SQL detection** (`rules.py`): now tokenizes comments and literals under each dialect's rules and judges each statement separately. Newly caught: `execute(command="DELETE FROM users")`, `psql -c '...'`, `DELETE u FROM users u`, `DROP/*!50000 TABLE*/`, `DROP#x\nTABLE`, `UPDATE "users" SET`, `UPDATE public.users SET`, `DELETE ... WHERE 1=1`, `DELETE ...; SELECT ... WHERE`, `DROP TEMPORARY/MATERIALIZED`, `ALTER TABLE a.b DROP`, `MERGE ... DELETE`, `RENAME TABLE`, `GRANT`, dynamic SQL, dict keys and object `__str__`. A `query` argument now counts as SQL. Every `DELETE FROM` stays auto-blocked, with or without `WHERE`. No longer falsely blocked: `SELECT 'DROP TABLE x'`, a statement inside a comment, and prose such as "we should truncate logs later". Optional `sqlglot` parser adds to these results and never removes from them
- **Nested policy rules** (`config.py`): custom `args_contain` / `args_regex` rules now scan nested arguments
- **Self-referencing arguments** (`rules.py`): no longer crash the bulk-operation rule with `RecursionError`
- **Timeout + retry** (`retry.py`, `timeout.py`): a retry after a thread-mode deadline keeps waiting for the attempt still running and uses its result, so a slow tool is never running twice at once. `RetryPolicy(overlap_on_timeout=True)` starts a new attempt for read-only tools that may hang. Async retries now use the full `max_retries`
- **Masking** (`masking.py`, `logger.py`): covers tuples (including `*args`), sets, bytes, dict keys and objects; adds Stripe, GitHub, GitLab, Slack, Google, Hugging Face, npm, SendGrid, AWS session keys, PEM blocks, URL credentials, short passwords and international phone numbers; epoch timestamps are no longer masked as cards or phones; `max_tokens`, `ping` and `mapping` no longer match sensitive keys; values nested under a sensitive key are redacted; every pattern is bounded, removing quadratic time on large output
- **Audit log** (`logger.py`): detects tail deletion, full deletion and nulled hashes (`seq` plus a chain head); a reused action id no longer silently drops the entry, which could lose a blocked action; writers in several processes can no longer fork the chain; logging failures are reported instead of swallowed
- **Webhook SSRF** (`webhooks.py`): connections are pinned to the validated addresses, closing DNS rebinding; IPv4-mapped, NAT64, 6to4, CGNAT, multicast and unspecified addresses are refused; proxies are honored with the same checks; a masking failure no longer sends the raw payload; 3xx and permanent 4xx are not retried, while 408, 425, 429, 5xx and failed DNS lookups are
- **Chaos** (`chaos.py`, `async_shield.py`): sync and async now behave the same. Chaos is inert in production by default; production injection requires an explicit `ChaosEngine(allow_production=True)` and raises a `RuntimeWarning`. Faults now really fire in test mode, `CORRUPT_OUTPUT` works, async latency no longer blocks the event loop and async timeouts fire `action_timeout`
- **Approval thresholds** (`approval.py`): `require_approval_for_risk("medium")` now covers HIGH/CRITICAL; `auto_deny=False` surfaces timeouts as ESCALATED instead of a silent denial
- **Rollback stack** (`snapshot.py`, `shield.py`): repeated `rollback()` calls undo one unrestored snapshot at a time; a deduplicated call no longer leaves an empty snapshot
- **Model-facing messages** (`integrations/`): all adapters share one model-safe message. `[ERROR] Output blocked: <violations>` no longer tells the model what was withheld, and secrets in tool errors are masked
- **Fallback gates** (`guards.py`): risk rules that key on the tool name now also see the fallback's name; a fallback's `actual_cost` is honored
- **Cost reconciliation** (`shield.py`): `reconcile_cost()` now also updates history, the estimator and anomaly detection, so an estimate that hid a spike raises an alert
- **Reservation ids** (`budget.py`): unique per attempt, so a reused action id cannot collide
- **Public API**: `WarderBox.execute()` is the adapter entry point; adapters and `replay.py` no longer call `_execute`
- **Wording**: "hard timeout" and "Hard deadline" described a thread timeout that keeps running; compliance reports are described as evidence, not certification

### Behavior changes to check when upgrading
- A guard that raises, such as a failed idempotency backend or state capture, now returns a blocked result instead of raising. The tool still does not run
- `history()` masks errors and block reasons; `history(raw=True)` returns the originals
- `stats()` and `budget_summary` no longer raise during a shared-state outage: they report this worker's view with `degraded: true`, while calls still fail closed
- `UPDATE` with a tautology such as `WHERE 1=1` now counts as having no `WHERE`
- Blocked messages reaching the model through adapters are now generic: the model is told the action was blocked, not which rule fired. The detailed reason stays in `history(raw=True)` and the audit log; output violations remain generic so they cannot quote withheld data
- Opening an existing unkeyed audit log with a key fails verification until `adopt_unkeyed_audit_history()` is called
- A duplicate action id is stored as `<id>#dup1` in the audit log
- Failed fallbacks are now recorded in the estimator, like failed primary calls

### Also added
- Audit-key enforcement is opt-in for backwards compatibility: existing keyless `WarderBox(db_path=...)` code keeps working. Set `require_audit_key=True` (or `require_audit_key: true` in a policy) in production to refuse startup without `audit_key=` / `WARDERBOX_AUDIT_KEY`
- A keyed audit log opened with a different key raises `AuditKeyError` before writing anything, so a mistyped key cannot mark a healthy log as damaged. Without a key a keyed log can be read but not written to
- In a policy, `audit_key_env` naming an unset variable and a non-boolean `require_audit_key` stop startup
- A blocked fallback keeps its `fallback_tool` in results, history, the audit log and events
- `KeyboardInterrupt` and `SystemExit` raised by a tool under a thread timeout reach the caller
- Search tools (`web_search`, `lookup`, ...) treat `query` as search text: only text that reads as a statement counts

### Known limits
- Rate-limit timestamps in `RedisStateBackend` come from each worker's clock
- Circuit breakers, estimator and anomaly detection are still per process
- Unkeyed audit logs detect naive edits only; see the README table for what each mode proves
- Masking and SQL detection are pattern based: a safety net, not a security boundary
- Process isolation needs `fork`, so it is unavailable on Windows
- In thread-mode timeouts a hung tool cannot be killed: retries wait on it, and report the timeout if it never finishes. Use process isolation or `overlap_on_timeout=True` to get a new attempt
- Behind a proxy with no local DNS, the proxy does the resolving, so it must keep internal hosts out of reach itself

## [0.3.6] - 2026-10-24

A large security & correctness hardening release over 0.3.5, consolidating an
extensive multi-pass review of the execution pipeline. 90+ new regression
tests (493 total). No signature-level public API breakage; behavior is now
fail-safe by default (notably capabilities fail closed and `risk=` is a floor).

### Security & correctness
- **Argument canonicalization** (`shield.py`, `rules.py`): tool arguments are bound to real parameter names via `inspect.signature`, and content rules scan nested structures — destructive-SQL / content rules can no longer be bypassed by positional or nested arguments
- **Risk override is a floor, not a ceiling** (`shield.py`): a per-tool `risk=` can only *raise* risk; declaring a tool LOW can never suppress a CRITICAL pattern detected in its arguments
- **Approval enforcement** (`shield.py`): `ApprovalManager` is wired into the sync and async pipelines; denied/timed-out approvals actually block execution
- **Capability enforcement** (`capabilities.py`, `multiagent.py`): `agent_name` is a first-class argument set by `AgentPool`; `CapabilityManager` defaults to **fail-closed** for unknown agents and is thread-safe
- **Log masking** (`logger.py`): `DataMasker` redacts tool output, errors and block reasons (not just arguments); sensitive keys (ssn, password, token, card, …) are redacted even when numeric/unformatted
- **Retry correctness** (`retry.py`, `shield.py`): fixed an off-by-one in retry counting; idempotency-protected tools don't auto-retry
- **Timeout composition** (`shield.py`): execution timeout applies on every attempt, including with a retry policy
- **Test mode** (`shield.py`): dry-run consumes no real budget/rate/quota state, but still evaluates every policy (including on the fallback path)

### Atomic idempotency & concurrency
- **Atomic idempotency** (`idempotency.py`, `shield.py`): `set_if_absent`/`begin`/`complete`/`fail` with an in-flight reservation (Redis uses SET NX) — concurrent calls and caller-level retries cannot double-fire a protected side effect
- **Atomic rate-limit & quota** (`ratelimit.py`, `quota.py`, `shield.py`): `reserve`/`release` close the concurrent-overrun race; `set_quota` preserves counters across restarts (durable quota backend)
- **Circuit breaker** (`circuitbreaker.py`): fixed a permanent HALF_OPEN lockout; reentrant lock + callbacks invoked outside the lock
- **Concurrency-safe history** (`shield.py`): `history()`/`stats()` snapshot under a lock
- **Thread-safety**: locks on `RateLimiter`, `CircuitBreaker`, `Tracer`, `AnomalyDetector`; bounded history, spans, alerts and budget entries; O(1) budget spend; thread-safe SQLite logger (WAL + lock)

### Async
- **Shared pipeline** (`shield.py`, `async_shield.py`): sync and async share one guard pipeline; async enforces the full guard set with budget reservation
- **Non-blocking** (`async_shield.py`): the pipeline runs via `asyncio.to_thread` so a human-approval wait never freezes the event loop; async fallbacks are awaited and bounded by timeout
- **Cancellation state machine** (`async_shield.py`): a cancelled task keeps its idempotency marker when execution had started (avoids duplicate side effects) and releases it otherwise; preflight is shielded so a mid-preflight cancel never orphans a reservation

### Fallback parity
- Fallbacks pass the full guard set — capability (on the fallback tool), risk floor, on_block, approval (for whichever tool carries the policy), budget/rate/quota reservation, timeout, output validation, idempotency — and are recorded in the estimator, anomaly detector, tracer, history (`history()["fallback"]`, `fallback_tool` DB column), and snapshot store (rollback/reversibility)
- **Fallback error hardening**: a raising output validator, fallback-function exception/`BaseException`, or idempotency backend failure no longer leaks budget/rate/quota reservations or leaves an unusable in-flight marker; the call returns a normal error result (or records the interruption before re-raising)

### Audit & interruption
- **Interrupted calls are audited**: sync `KeyboardInterrupt` and async cancellation/`BaseException` now write an error result to history and close the tracer span before propagating; budget/rate/quota are released
- **Idempotency backend failures** (`complete`/`fail`) emit an `idempotency_error` event instead of turning a completed call into a caller-visible exception

### Side-channel isolation
- A faulty/custom **estimator, tracer, audit logger, anomaly detector**, or any **callback** (`on_open`/`on_close`, `on_block`, `on_alert`, approval request) can no longer crash or corrupt the tool call it observes. `on_block` fails closed. Reservations are released on preflight exceptions and sync interruption (`KeyboardInterrupt`)

### Reporting, adapters & tooling
- **Compliance** (`compliance.py`): epoch-aware date filtering, correct hourly distribution, case-insensitive CRITICAL anomaly detection, schema guard on fresh DBs, HTML escaping
- **Forecast** (`forecast.py`): epoch timestamps + floored observation window → realistic burn rates
- **Scoring** (`scoring.py`): risk levels normalized so critical attempts are penalized
- **CLI/dashboard** (`cli.py`, `shield.py`): `stats` no longer crashes on empty/populated DBs; the CLI dashboard loads history and rebuilds budget from the database (idempotently)
- **Framework adapters** (`integrations/`): Anthropic tool_result ids match the originating tool_use; LangChain/CrewAI pass string inputs positionally; replay unpacks legacy positional captures
- **Packaging/CI**: `py.typed`, GitHub Actions matrix (3.11–3.13: ruff + tests + build + demo), `RedisBackend`, `Mode` exported, ruff clean

## [0.3.5] - 2026-10-21

### Changed
- License moved to PolyForm Noncommercial 1.0.0 (commercial license available); PyPI metadata reads the LICENSE file directly

## [0.3.4] - 2026-10-20

### Changed
- Packaging and description refinements; license classifier updates

## [0.3.3] - 2026-10-19

### Added — Community Feedback + Cost Intelligence
- **Pre-execution Cost Estimation** (`estimator.py`): Predicts cost and duration BEFORE execution with best/expected/worst case. Self-improving — accuracy increases as more data is recorded. Confidence score from 0% to 95%
- **Live Estimation Dashboard**: Real-time Estimation tab on FinOps dashboard — per-tool confidence tracking, self-improving accuracy milestones, cost/duration averages updating every 3 seconds
- **Rollback Transparency** (`shield.py`): `reversible` flag on tool decorator — auto-inferred from snapshot functions, or set explicitly. Every action carries reversibility info in `history()`
- **Client-supplied Idempotency Keys** (`idempotency.py`): Callers can supply their own idempotency key instead of relying on auto-generated content hashes — enables distributed deduplication across processes
- **Pluggable Idempotency Backend** (`idempotency.py`): `IdempotencyBackend` abstract class with `MemoryBackend` (default). Swap in Redis or any external store for cross-process/distributed dedup
- **Routing Benchmark** (`benchmarks/routing_benchmark.py`): Reproducible benchmark comparing cost vs quality with and without smart fallback routing — shows +40% success rate improvement
- **40 new tests**: 22 estimator + 9 reversibility + 9 idempotency (client keys + backend)
- Total test count: **390 tests**
- Total modules: **42**

## [0.3.2] - 2026-10-16

### Added — Production Hardening
- **Execution Timeout** (`timeout.py`): Hard timeout enforcement for tool calls, per-tool overrides, daemon-thread execution, prevents hanging calls from burning budget
- **Idempotency Guard** (`idempotency.py`): Duplicate execution prevention with content-hash keys, TTL-based expiry, per-tool protection, cached result return — prevents double-sends on retry
- **Output Validation** (`output_validation.py`): Validates/redacts/blocks tool outputs before they reach the agent, pattern-based PII detection, max-length enforcement, custom validators, built-in PII rules
- **Fallback Routing** (`fallback.py`): Automatic fallback to cheaper/safer tools when circuit breaker opens, configurable conditions, max-fallback limits, graceful degradation
- **Execution Quotas** (`quota.py`): Daily/weekly/monthly call limits per tool or globally, complements sliding-window rate limiter with long-term caps
- **85 new tests**: Unit + integration tests for all 5 new modules
- Total test count: **350 tests**
- Total modules: **41**

## [0.3.1] - 2026-10-16

### Added — FinOps & Agent Cost Optimization
- **Smart Model Routing** (`routing.py`): Auto-routes tasks to cheapest capable LLM model, complexity classification (LOW/MEDIUM/HIGH), tool overrides, 60-90% cost savings
- **Cost Anomaly Detection** (`anomaly.py`): Z-score based cost spike detection, infinite loop detection, rate spike monitoring, burn rate alerts, per-tool tracking, callbacks
- **FinOps Dashboard**: 5-tab real-time web UI (Overview, FinOps, Estimation, Routing, Alerts) with cost charts, model distribution, alert severity
- **Thread-safe Budget Reservation** (`budget.py`): Reserve-before-dispatch with lock, cost reconciliation with actual provider charges, drift tracking
- **100 integration tests**: Comprehensive tests exercising all 36 modules simultaneously
- Total test count: **265 tests**

## [0.3.0] - 2026-10-15

### Added — Initial Public Release
- PyPI package: `pip install warderbox`
- Full module suite with 42 production-grade safety modules

## [0.2.0] - 2026-10-08

### Added — Phase 7: Enterprise Safety
- **Circuit Breaker** (`circuitbreaker.py`): CLOSED/OPEN/HALF_OPEN states, configurable failure threshold, reset timeout, success threshold for recovery, on_open/on_close callbacks
- **Approval Workflows** (`approval.py`): Human-in-the-loop with configurable timeout, auto-deny, callback-based approve/deny, risk-based approval, escalation chains
- **Agent Capabilities** (`capabilities.py`): Per-agent allowlist/denylist, argument constraints (args_deny/args_require), max cost per tool, max calls per hour, time-based schedules
- **Compliance Reports** (`compliance.py`): Structured audit reports from SQLite logs, anomaly detection (high block/error rate, CRITICAL attempts), risk breakdown, JSON/HTML export

### Added — Phase 8: Observability & Resilience
- **Distributed Tracing** (`tracing.py`): OpenTelemetry-compatible spans with trace/span IDs, nested spans via context manager, JSON and OTLP export, trace grouping
- **Chaos Engineering** (`chaos.py`): Fault injection (ERROR, LATENCY, TIMEOUT, CORRUPT_OUTPUT), probability control, conditional triggers, wildcard rules, max injection limits
- **Cost Forecasting** (`forecast.py`): Burn rate analysis (per minute/hour), budget exhaustion prediction, trend detection (increasing/decreasing/stable), cost-by-tool breakdown
- **Agent Scoring** (`scoring.py`): Safety/reliability/efficiency/risk dimensions (0-100), A-F grading, configurable weights, multi-agent pool ranking via PoolScorer

### Added — Phase 9: Polish & Market Readiness
- Full README rewrite covering all 32+ modules
- Landing page (`site/index.html`) with feature showcase and market data
- Comprehensive end-to-end demo (`examples/demo_full.py`)
- This CHANGELOG

### Changed
- `shield.py`: Integrated circuit breaker, capabilities, chaos, and tracing into execution pipeline
- `__init__.py`: Added exports for all new modules (ApprovalManager, CapabilityManager, CircuitBreaker, ChaosEngine, ComplianceReporter, CostForecaster, AgentScorer, PoolScorer, Tracer, FaultType, CircuitState)

## [0.1.0] - 2026-10-08

### Added — Phase 1: Core
- `WarderBox` main class with tool decorator
- Risk classification engine (LOW/MEDIUM/HIGH/CRITICAL)
- Budget tracking with per-action cost
- State snapshots and rollback
- Test/simulation mode

### Added — Phase 2: Infrastructure
- Dict-based configuration system
- SQLite-backed action logging
- OpenAI Agents SDK integration
- LangChain integration
- CLI tool (stats, logs, export, validate, dashboard)
- pip install support via pyproject.toml

### Added — Phase 3: Rate Limiting & Events
- Sliding-window rate limiter (global RPM/RPH, per-tool limits)
- Webhook/event notification system (HTTP + callbacks)
- Anthropic Claude SDK integration
- Async support with async_tool decorator

### Added — Phase 4: Policy & Dashboard
- Policy-as-code (JSON/YAML files)
- Retry with exponential backoff and conditional retry
- CrewAI integration
- Web dashboard (dark theme, auto-refresh, stats cards)

### Added — Phase 5: Multi-Agent & Metrics
- AgentPool for multi-agent management with isolated budgets
- Microsoft AutoGen integration
- Prometheus-compatible metrics export (OpenMetrics text format)

### Added — Phase 6: Privacy & Debug
- DataMasker with 10 built-in PII/secret patterns
- Custom masking patterns support
- ActionReplayer for debugging/audit from SQLite logs
- Dry-run and single-action replay
