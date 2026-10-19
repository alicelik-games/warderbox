"""WarderBox demo — shows rollback, cost control, and test mode in action."""

import sys
sys.path.insert(0, "src")

from warderbox import WarderBox

# ============================================================
# 1. Simulated database (in-memory)
# ============================================================
database = {
    "users": [
        {"id": 1, "name": "Ali", "email": "ali@example.com"},
        {"id": 2, "name": "Ayse", "email": "ayse@example.com"},
        {"id": 3, "name": "Mehmet", "email": "mehmet@example.com"},
    ]
}


def get_db_state():
    """Capture current database state for rollback."""
    import copy
    return copy.deepcopy(database)


def restore_db_state(state):
    """Restore database from snapshot."""
    database.clear()
    database.update(state)


# ============================================================
# 2. Create shield with $1.00 budget
# ============================================================
shield = WarderBox(
    budget=1.00,
    auto_snapshot=True,
    mode="production",
)


# ============================================================
# 3. Define tools (agent actions)
# ============================================================
@shield.tool(cost=0.01, snapshot_state=get_db_state, restore_state=restore_db_state)
def list_users():
    """List all users."""
    return database["users"]


@shield.tool(cost=0.02, snapshot_state=get_db_state, restore_state=restore_db_state)
def add_user(name: str, email: str):
    """Add a new user."""
    new_id = max(u["id"] for u in database["users"]) + 1
    user = {"id": new_id, "name": name, "email": email}
    database["users"].append(user)
    return f"Added user {name} (id={new_id})"


@shield.tool(cost=0.02, snapshot_state=get_db_state, restore_state=restore_db_state)
def delete_user(user_id: int):
    """Delete a user by ID."""
    before = len(database["users"])
    database["users"] = [u for u in database["users"] if u["id"] != user_id]
    after = len(database["users"])
    return f"Deleted {before - after} user(s)"


@shield.tool(cost=0.05, snapshot_state=get_db_state, restore_state=restore_db_state)
def run_sql(query: str):
    """Simulate running a SQL query."""
    return f"Executed: {query}"


# ============================================================
# DEMO
# ============================================================
def main():
    print("=" * 60)
    print("  WarderBox Demo")
    print("=" * 60)

    # -- Normal operations --
    print("\n1. List users:")
    result = list_users()
    print(f"   {result.output}")

    print("\n2. Add a new user:")
    result = add_user(name="Zeynep", email="zeynep@example.com")
    print(f"   {result.output}")
    print(f"   Users now: {[u['name'] for u in database['users']]}")

    # -- Dangerous operation: blocked automatically --
    print("\n3. Try DROP TABLE (CRITICAL risk):")
    result = run_sql(query="DROP TABLE users")
    print(f"   Blocked: {result.blocked}")
    print(f"   Reason: {result.block_reason}")

    # -- Delete a user, then rollback --
    print("\n4. Delete user Mehmet:")
    result = delete_user(user_id=3)
    print(f"   {result.output}")
    print(f"   Users now: {[u['name'] for u in database['users']]}")

    print("\n5. Oops! Rollback:")
    success = shield.rollback()
    print(f"   Rollback success: {success}")
    print(f"   Users now: {[u['name'] for u in database['users']]}")

    # -- Budget check --
    print("\n6. Budget summary:")
    summary = shield.budget_summary
    print(f"   Spent: ${summary['spent']:.2f} / ${summary['budget']:.2f}")
    print(f"   Remaining: ${summary['remaining']:.2f}")

    # -- History --
    print("\n7. Action history:")
    for entry in shield.history():
        status = "BLOCKED" if entry["blocked"] else "OK"
        print(f"   [{status}] {entry['tool']} (risk={entry['risk']}, cost=${entry['cost']:.2f})")

    # -- Stats --
    print("\n8. Stats:")
    stats = shield.stats()
    print(f"   Total actions: {stats['total_actions']}")
    print(f"   Blocked: {stats['blocked']}")
    print(f"   Successful: {stats['successful']}")
    print(f"   Snapshots: {stats['snapshots']}")

    # -- Cost Estimation (self-improving) --
    print("\n9. Pre-execution cost estimation:")
    plan = ["list_users", "add_user", "run_sql"]
    estimate = shield.estimate(plan)
    est = estimate.summary()
    print(f"   Plan: {plan}")
    print(f"   Expected cost: ${est['cost']['expected']}")
    print(f"   Worst case:    ${est['cost']['worst']}")
    print(f"   Confidence:    {est['confidence']:.0%}")
    print(f"   Data points:   {est['data_points']}")
    print("   (Self-improving — gets smarter the more your agents run)")

    print("\n" + "=" * 60)
    print("  Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
