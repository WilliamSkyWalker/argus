"""CLI for explicit workflows; no LLM key needed."""

import json
import os
from pathlib import Path

from .engine import Runtime
from .store import Store


def register(subparsers):
    parser = subparsers.add_parser("workflow", help="Durable multi-resource workflows")
    parser.add_argument("--state-dir", default=os.environ.get("SAYGO_RUNTIME_DIR", ".saygo_runs/runtime"),
                        help="Shared runtime state directory (use the same directory for all commands)")
    sub = parser.add_subparsers(dest="workflow_command", required=True)
    for command in ("start", "create"):
        p = sub.add_parser(command, help="Validate and " + ("execute" if command == "start" else "save") + " a workflow")
        p.add_argument("file")
    for command in ("run", "status", "events", "pause", "cancel", "recover"):
        p = sub.add_parser(command)
        p.add_argument("run_id")
    p = sub.add_parser("resume", help="Return control after manual work; re-observe before continuing")
    p.add_argument("run_id")
    p.add_argument("--note", required=True)
    p.add_argument("--data", default="{}", help="JSON object returned by the user, e.g. an order ID")
    p = sub.add_parser("resolve", help="Reconcile an uncertain action without replaying it")
    p.add_argument("run_id")
    p.add_argument("--outcome", choices=["completed", "not_executed"], required=True)
    p.add_argument("--note", required=True)


def dispatch(args):
    store = Store(args.state_dir)
    runtime = Runtime(store)
    command = args.workflow_command
    try:
        if command in {"start", "create"}:
            path = Path(args.file).resolve()
            state = runtime.create(json.loads(path.read_text()), base_dir=path.parent)
            # Emit the durable ID before an action can fail or the process can be killed.
            if command == "start":
                print(json.dumps({"created_run_id": state["id"]}), flush=True)
                state = runtime.run(state["id"])
        elif command == "status":
            state = store.get(args.run_id)
        elif command == "events":
            print(json.dumps(store.events(args.run_id), ensure_ascii=False, indent=2))
            return
        elif command == "run":
            state = runtime.run(args.run_id)
        elif command == "resume":
            state = runtime.resume(args.run_id, args.note, json.loads(args.data))
        elif command == "recover":
            state = runtime.recover(args.run_id)
        elif command == "resolve":
            state = runtime.resolve_action(args.run_id, args.outcome, args.note)
        elif command == "cancel":
            state = runtime.cancel(args.run_id)
        elif command == "pause":
            store.request(args.run_id, "pause")
            state = store.get(args.run_id)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        if state["status"] in {"failed", "needs_review"}:
            raise SystemExit(1)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
