"""Demo: WarderBox with OpenAI-style agent loop.

This simulates how WarderBox works with OpenAI's function calling,
without requiring an actual OpenAI API key.
"""

import sys
sys.path.insert(0, "src")

from warderbox import WarderBox
from warderbox.integrations.openai_agents import OpenAIShieldMiddleware


# ============================================================
# 1. Simulated environment
# ============================================================
files = {
    "/data/report.csv": "id,name,revenue\n1,Alpha,50000\n2,Beta,75000",
    "/data/config.json": '{"db_host": "prod-db.example.com", "key": "secret123"}',
    "/logs/app.log": "2026-10-08 INFO: Server started\n2026-10-08 ERROR: Timeout",
}

emails_sent = []


# ============================================================
# 2. Define tool functions
# ============================================================
def read_file(path: str) -> str:
    if path in files:
        return files[path]
    return f"File not found: {path}"


def delete_file(path: str) -> str:
    if path in files:
        del files[path]
        return f"Deleted: {path}"
    return f"File not found: {path}"


def send_email(to: str, subject: str, body: str) -> str:
    emails_sent.append({"to": to, "subject": subject, "body": body})
    return f"Email sent to {to}"


def run_sql(query: str) -> str:
    return f"Query executed: {query}"


# ============================================================
# 3. Set up WarderBox with config
# ============================================================
shield = WarderBox.from_config({
    "budget": 2.00,
    "mode": "production",
    "db_path": "demo_audit.db",
    "rules": [
        {
            "name": "no_config_delete",
            "match": {"tool": "delete_file", "args_contain": "config"},
            "risk": "critical",
            "reason": "Cannot delete configuration files",
        },
        {
            "name": "no_prod_sql",
            "match": {"args_regex": r"prod|production"},
            "risk": "high",
            "reason": "Production database queries require approval",
        },
    ],
})


# ============================================================
# 4. Create middleware
# ============================================================
middleware = OpenAIShieldMiddleware(
    shield=shield,
    executors={
        "read_file": lambda **kw: read_file(**kw),
        "delete_file": lambda **kw: delete_file(**kw),
        "send_email": lambda **kw: send_email(**kw),
        "run_sql": lambda **kw: run_sql(**kw),
    },
    cost_map={
        "read_file": 0.01,
        "delete_file": 0.02,
        "send_email": 0.05,
        "run_sql": 0.03,
    },
)


# ============================================================
# 5. Simulate tool calls (as if from OpenAI API response)
# ============================================================
class FakeToolCall:
    """Mimics OpenAI's tool_call object."""
    def __init__(self, name: str, arguments: str):
        self.function = type("F", (), {"name": name, "arguments": arguments})()


def main():
    print("=" * 60)
    print("  WarderBox + OpenAI Integration Demo")
    print("=" * 60)

    # Safe: read a file
    print("\n1. Agent reads a file:")
    result = middleware.handle_tool_call(
        FakeToolCall("read_file", '{"path": "/data/report.csv"}')
    )
    print(f"   Output: {result.output[:60]}...")
    print(f"   Cost: ${result.cost:.2f}")

    # Blocked: delete config file (custom rule)
    print("\n2. Agent tries to delete config.json:")
    result = middleware.handle_tool_call(
        FakeToolCall("delete_file", '{"path": "/data/config.json"}')
    )
    print(f"   Blocked: {result.blocked}")
    print(f"   Reason: {result.block_reason}")

    # Safe: delete a log file
    print("\n3. Agent deletes a log file:")
    result = middleware.handle_tool_call(
        FakeToolCall("delete_file", '{"path": "/logs/app.log"}')
    )
    print(f"   Output: {result.output}")
    print(f"   Files remaining: {list(files.keys())}")

    # Blocked: destructive SQL
    print("\n4. Agent tries DROP TABLE:")
    result = middleware.handle_tool_call(
        FakeToolCall("run_sql", '{"query": "DROP TABLE customers"}')
    )
    print(f"   Blocked: {result.blocked}")
    print(f"   Reason: {result.block_reason}")

    # Safe: normal SQL
    print("\n5. Agent runs safe query:")
    result = middleware.handle_tool_call(
        FakeToolCall("run_sql", '{"query": "SELECT COUNT(*) FROM orders"}')
    )
    print(f"   Output: {result.output}")

    # Send email
    print("\n6. Agent sends email:")
    result = middleware.handle_tool_call(
        FakeToolCall("send_email", '{"to": "team@example.com", "subject": "Report", "body": "Q3 is done"}')
    )
    print(f"   Output: {result.output}")
    print(f"   Emails sent: {len(emails_sent)}")

    # Stats
    print("\n" + "-" * 60)
    print("  Final Stats")
    print("-" * 60)
    stats = shield.stats()
    print(f"  Total actions:  {stats['total_actions']}")
    print(f"  Blocked:        {stats['blocked']}")
    print(f"  Successful:     {stats['successful']}")
    budget = stats["budget"]
    print(f"  Budget:         ${budget['spent']:.2f} / ${budget['budget']:.2f}")

    # Audit log
    print(f"\n  Audit log saved to: demo_audit.db")
    db_stats = shield._logger.stats()
    print(f"  DB records:     {db_stats['total']}")

    shield._logger.close()

    # Cleanup
    import os
    if os.path.exists("demo_audit.db"):
        os.unlink("demo_audit.db")

    print("\n" + "=" * 60)
    print("  Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
