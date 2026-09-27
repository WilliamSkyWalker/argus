"""Session diagnostics and explicit project resource aliases."""
import importlib.util
import json
from pathlib import Path


def register(sub):
    p = sub.add_parser("doctor", help="Diagnose a connected session without sending input")
    p.add_argument("--session")
    p.add_argument("--probe-action", type=json.loads, help="Explicit harmless input action to test; requires --session")
    p.set_defaults(handler=doctor)
    p = sub.add_parser("resources", help="Project resource aliases in .saygo/resources.json")
    p.add_argument("--config", default=".saygo/resources.json")
    commands = p.add_subparsers(dest="resource_command", required=True)
    commands.add_parser("list")
    bind = commands.add_parser("bind")
    bind.add_argument("name")
    bind.add_argument("session")
    p.set_defaults(handler=resources)


def diagnose(session=None, probe_action=None):
    from saygo.devices.control import sessions
    from saygo.devices.service import execute
    result = {"sessions": sessions(), "python_dependencies": {name: importlib.util.find_spec(name) is not None
              for name in ("PIL", "playwright", "selenium", "appium", "mcp")}}
    if probe_action is not None and not session:
        raise ValueError("--probe-action requires --session")
    if session:
        result["diagnostics"] = execute("diagnose", session)
        result["capabilities"] = execute("capabilities", session)
        shot = execute("screenshot", session)
        result["observation"] = shot
        result["image_readable"] = False
        if shot.get("ok"):
            from PIL import Image
            with Image.open(shot["path"]) as im:
                im.verify()
            result["image_readable"] = True
        result["input_check"] = {"status": "not_tested", "reason": "Input requires a user-chosen harmless target; capabilities do not prove input permission"}
        if probe_action is not None:
            result["input_check"] = execute("act", session, action=probe_action, observe_after=True)
        result["ok"] = result["image_readable"] and result["capabilities"].get("ok", False) and result["input_check"].get("ok", True)
    return result


def doctor(args):
    result = diagnose(args.session, args.probe_action)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("ok") is False:
        raise SystemExit(2)


def resources(args):
    from saygo.platforms import device_session as ds
    path = Path(args.config)
    values = json.loads(path.read_text()) if path.exists() else {}
    if args.resource_command == "bind":
        if not ds.load_state(args.session):
            raise SystemExit("Connect the named session first")
        values[args.name] = args.session
        path.parent.mkdir(parents=True, exist_ok=True)
        import os, tempfile
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".resources-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists(): Path(temporary).unlink()
    print(json.dumps(values, ensure_ascii=False))
