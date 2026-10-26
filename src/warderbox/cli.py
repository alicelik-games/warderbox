"""WarderBox CLI — view audit logs and stats from the command line."""

from __future__ import annotations

import argparse
import json
import sys

from warderbox.logger import ActionLogger
from warderbox.policy import validate_policy


def cmd_stats(args: argparse.Namespace) -> None:
    logger = ActionLogger(args.db)
    s = logger.stats()
    print(f"Total actions:  {s['total']}")
    print(f"Blocked:        {s['blocked']}")
    print(f"Errors:         {s['errors']}")
    print(f"Total cost:     ${s['total_cost']:.2f}")
    print(f"Top tools:      {json.dumps(s['by_tool'], indent=2)}")
    logger.close()


def cmd_logs(args: argparse.Namespace) -> None:
    logger = ActionLogger(args.db)
    rows = logger.query(
        tool_name=args.tool,
        blocked_only=args.blocked,
        limit=args.limit,
    )
    for row in rows:
        status = "BLOCKED" if row["blocked"] else "OK"
        risk = row.get("risk", "?")
        cost = row.get("cost", 0)
        tool = row["tool_name"]
        if row.get("fallback_tool"):
            tool = f"{tool} -> {row['fallback_tool']}"
        print(f"[{status}] {tool} | risk={risk} cost=${cost:.2f} | {row['args'][:80]}")
        if row.get("block_reason"):
            print(f"         reason: {row['block_reason']}")
        if row.get("error"):
            print(f"         error: {row['error']}")
    logger.close()


def cmd_export(args: argparse.Namespace) -> None:
    logger = ActionLogger(args.db)
    count = logger.export_json(args.output)
    print(f"Exported {count} records to {args.output}")
    logger.close()


def cmd_validate(args: argparse.Namespace) -> None:
    issues = validate_policy(args.policy_file)
    if not issues:
        print(f"Policy file is valid: {args.policy_file}")
    else:
        print(f"Issues found in {args.policy_file}:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)


def cmd_dashboard(args: argparse.Namespace) -> None:
    from warderbox import WarderBox
    from warderbox.dashboard import start_dashboard

    shield = WarderBox(db_path=args.db)
    loaded = shield.load_history_from_db()

    print(f"WarderBox Dashboard: http://127.0.0.1:{args.port} ({loaded} actions from {args.db})")
    start_dashboard(shield, port=args.port, background=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="warderbox",
        description="WarderBox — audit logs and stats for AI agent actions",
    )
    parser.add_argument("--db", default="warderbox.db", help="Path to SQLite database")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("stats", help="Show action statistics")

    logs_p = sub.add_parser("logs", help="View action logs")
    logs_p.add_argument("--tool", help="Filter by tool name")
    logs_p.add_argument("--blocked", action="store_true", help="Show only blocked actions")
    logs_p.add_argument("--limit", type=int, default=50, help="Max rows to show")

    export_p = sub.add_parser("export", help="Export logs to JSON")
    export_p.add_argument("output", help="Output JSON file path")

    validate_p = sub.add_parser("validate", help="Validate a policy file")
    validate_p.add_argument("policy_file", help="Path to policy YAML/JSON file")

    dashboard_p = sub.add_parser("dashboard", help="Start web dashboard")
    dashboard_p.add_argument("--port", type=int, default=8080, help="Port (default 8080)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "stats": cmd_stats,
        "logs": cmd_logs,
        "export": cmd_export,
        "validate": cmd_validate,
        "dashboard": cmd_dashboard,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
