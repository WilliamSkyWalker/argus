"""Incremental external-agent task CLI."""
import json


def register(sub):
    parser = sub.add_parser("task", help="Durable interactive tasks across named sessions")
    commands = parser.add_subparsers(dest="task_command", required=True)
    commands.add_parser("list")
    create = commands.add_parser("create")
    create.add_argument("bindings", nargs="?", type=json.loads, help='JSON mapping, e.g. {"phone":"test-phone","web":"mail"}')
    for command in ("status", "events", "timeline", "observe", "diagnose", "repair", "submit", "recover", "resolve", "handoff", "resume", "finish", "cancel", "export"):
        p = commands.add_parser(command)
        p.add_argument("task_id")
        if command in {"observe", "submit", "diagnose", "repair"}:
            p.add_argument("--resource", required=True)
        if command == "repair":
            p.add_argument("--operation", choices=["reobserve", "select_window"], required=True)
            p.add_argument("--window-id")
            p.add_argument("--note", required=True)
        if command == "submit":
            p.add_argument("--action", type=json.loads, required=True)
            p.add_argument("--observation-id", required=True)
            p.add_argument("--request-id", required=True)
            p.add_argument("--note")
        if command in {"resolve", "resume", "finish", "cancel"}:
            p.add_argument("--note", required=True)
        if command == "resolve":
            p.add_argument("--outcome", choices=["completed", "not_executed"], required=True)
        if command == "handoff":
            p.add_argument("--instructions", required=True)
        if command == "export":
            p.add_argument("--out", required=True)
    parser.set_defaults(handler=dispatch)


def dispatch(args):
    from argus.runtime.interactive import call
    options = vars(args).copy()
    for key in ("command", "task_command", "task_id", "handler", "verbose"):
        options.pop(key, None)
    try:
        if args.task_command == "create" and options.get("bindings") is None:
            from pathlib import Path
            options["bindings"] = json.loads(Path(".argus/resources.json").read_text())
        result = call(args.task_command, getattr(args, "task_id", None), **options)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("status") == "needs_review" or result.get("error"):
            raise SystemExit(1)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(2) from exc
