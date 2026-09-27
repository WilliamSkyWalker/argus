"""Shared CLI/MCP operation service. Never caches a controller across calls."""
import time
from pathlib import Path
from argparse import Namespace


def _request(command, session=None, **options):
    defaults = dict(out=None, foreground=False, wait_s=0, backend=None, os="android",
                    prepare_only=False, replace=False, send_x=None, send_y=None,
                    force_stop=False, duration_ms=300, crop=None, observation_id=None,
                    coordinate_space="screen", observe_after=False, timeout=5, mode="stable", window_id=None)
    defaults.update(options)
    result = _execute(Namespace(device_command=command, serial=session, **defaults))
    if result is None:
        return {"ok": False, "error": f"Unsupported command: {command}", "error_type": "ValueError"}
    result.setdefault("ok", True)
    result.setdefault("session", session or "default")
    if result["ok"] and command in {"tap", "swipe", "input", "key", "open", "scroll", "navigate", "launch", "type-send", "new-page", "select-page", "close-page"}:
        result.update(dispatched=True, business_success=None, requires_observation=True)
    return result


def _execute(args):
    """Execute against the persisted binding and release the temporary controller."""
    from saygo.platforms import device_session as ds

    cmd = args.device_command
    serial = getattr(args, "serial", None)

    def _shot(plat, out_path):
        from .observations import capture
        return capture(plat, serial, out_path, crop=args.crop)

    plat = None
    submission_attempted = False
    dispatched = False
    supported = {"start", "stop", "pages", "select-page", "close-page", "new-page", "open", "scroll",
                 "navigate", "screenshot", "focus", "tap", "swipe", "input", "type-send", "key", "launch",
                 "capabilities", "diagnose", "wait", "act"}
    try:
        import math
        if not math.isfinite(args.wait_s) or not 0 <= args.wait_s <= 60:
            raise ValueError("wait_s must be finite and in [0,60]")
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
            raise ValueError("timeout must be finite and in (0,60]")
        if cmd not in supported:
            raise ValueError(f"Unsupported command: {cmd}")
        state = ds.load_state(serial)
        if state and state.get("handoff"):
            raise RuntimeError("Session waiting_for_human; resume before operating")
        if cmd == "start":
            os_name = getattr(args, "os", "android")
            if state and (state.get("os") or state.get("kind")) != os_name:
                raise ValueError("Session belongs to another platform; choose another session")
            plat = ds.start(serial, os_name, browser_backend=getattr(args, "backend", None))
            sw, sh = plat.screen_size
            return ({"ok": True, "serial": serial or "default",
                  "os": getattr(plat, "_os", os_name),
                  "session_id": getattr(getattr(plat, "_driver", None), "session_id", None),
                  "page_id": getattr(plat, "page_id", None), "screen_size": [sw, sh]})
        if cmd == "stop":
            ds.stop(serial)
            return ({"ok": True, "stopped": serial or "default"})

        state = ds.load_state(serial)
        if state and state.get("handoff"):
            raise RuntimeError("Session waiting_for_human; resume before operating")
        if cmd in ("pages", "select-page", "close-page", "new-page"):
            state = ds.load_state(serial) or {}
            backend = state.get("browser_backend", "selenium")
            if backend == "selenium":
                raise ValueError("Page management requires Playwright or extension")
            plat = ds.attach_browser(serial, backend=backend, manage_pages=True)
            if cmd != "pages":
                before = _page_evidence(plat, serial, args.record, "before")
                _record(args.record, "dispatching", {"before": before})
            if cmd == "new-page":
                dispatched = True
                page_id = plat.new_page(args.url)
                return ({"created_page_id": page_id, "selected_page_id": plat.page_id, "pages": plat.list_pages()})
            if cmd == "select-page":
                dispatched = True
                plat.select_page(args.page_id)
                state = ds.load_state(serial)
                state["browser_backend"] = backend
                ds.save_state(serial, state)
            elif cmd == "close-page":
                dispatched = True
                plat.close_page(args.page_id)
            return ({"pages": plat.list_pages(), "selected_page_id": plat.page_id})

        # A saved browser must reconnect as-is; never replace it after a selection error.
        state = ds.load_state(serial)
        if state and state.get("disconnected"):
            raise RuntimeError("Session disconnected; use device connect to reconnect")
        if state and state.get("handoff"):
            raise RuntimeError("Session waiting_for_human; resume before operating")
        foreground = getattr(args, "foreground", False)
        if args.window_id is not None and (not state or state.get('os') != 'windows'):
            raise ValueError('--window-id requires a connected Windows desktop session')
        if foreground and (not state or state.get("kind") != "desktop" or state.get("os") != "windows"):
            raise ValueError("--foreground requires a connected Windows desktop session")
        if cmd == "type-send" and not args.prepare_only and (args.send_x is None or args.send_y is None):
            raise ValueError("type-send requires --send-x and --send-y unless --prepare-only is set")
        if state and state.get("kind") == "desktop":
            extra = {"window_id": args.window_id} if args.window_id is not None else {}
            plat = ds.attach_desktop(state, serial=serial, foreground=foreground, **extra)
        elif state and state.get("kind") == "browser":
            plat = ds.attach_browser(serial)
        else:
            plat = ds.attach(serial)
            if plat is None:
                raise RuntimeError("Session missing or expired; explicitly connect/start it before operating")

        from . import observations, actions
        if cmd == "diagnose":
            from .diagnostics import collect
            return collect(plat)
        if cmd == "capabilities":
            return actions.capabilities(plat)
        if cmd == "wait":
            result = observations.wait(plat, args.mode, args.timeout)
            result["observation"] = _shot(plat, args.out)
            return result
        if cmd == "act":
            observation = observations.load(args.observation_id, serial) if args.observation_id else None
            if observation and time.time() - observation["at"] > 30:
                raise ValueError("Observation expired; observe again (maximum age 30s)")
            prepared = actions.prepare(plat, args.action, observation)
            before = _shot(plat, None)
            if not observation and before.get("window_id") is not None and callable(getattr(type(plat), "expect_window", None)):
                plat.expect_window(before)
            _record(args.record, "dispatching", {"action": prepared, "before": before})
            dispatched = True
            try:
                result = actions.dispatch(plat, prepared)
            except Exception as exc:
                from .diagnostics import collect
                return {"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                        "outcome": "uncertain", "requires_observation": True,
                        "diagnostics": collect(plat, exc)}
            _record(args.record, "dispatched", result)
            if args.observe_after:
                result["wait"] = observations.wait(plat, "stable", args.timeout)
                result["observation"] = _shot(plat, args.out)
            return result
        if cmd not in {"screenshot", "focus"}:
            before = _shot(plat, None)
            if before.get("window_id") is not None and callable(getattr(type(plat), "expect_window", None)):
                plat.expect_window(before)
            _record(args.record, "dispatching", {"before": before})

        if cmd == "open":
            dispatched = True
            plat.open_target(args.target)
            return ({"ok": True, "target": args.target})
        elif cmd == "scroll":
            dispatched = True
            (plat.scroll_up if args.direction == "up" else plat.scroll_down)()
            return ({"ok": True, "direction": args.direction})
        elif cmd == "navigate":
            dispatched = True
            plat.open_target(args.url)
            time.sleep(max(0.0, args.wait_s))
            return (_shot(plat, args.out))
        elif cmd in ("screenshot", "focus"):
            return (_shot(plat, args.out))
        elif cmd == "tap":
            observation = observations.load(args.observation_id, serial) if args.observation_id else None
            prepared = actions.prepare(plat, {"type":"tap", "x":args.x, "y":args.y,
                                             "coordinate_space":args.coordinate_space}, observation)
            dispatched = True
            plat.tap(prepared["x"], prepared["y"])
            result = {"ok": True, "tapped": [args.x, args.y]}
            if args.out or args.observe_after:
                observations.wait(plat, "stable", args.timeout)
                result.update(_shot(plat, args.out))
                result["requires_observation"] = True
            return (result)
        elif cmd == "swipe":
            if not 1 <= args.duration_ms <= 30000:
                raise ValueError("duration_ms must be in [1,30000]")
            if callable(getattr(type(plat), "_w3c_swipe", None)):
                dispatched = True
                plat._w3c_swipe(args.x1, args.y1, args.x2, args.y2, duration_ms=args.duration_ms)
            elif args.duration_ms != 300:
                raise ValueError("Custom swipe duration is unsupported by this backend")
            else:
                dispatched = True
                plat.swipe(args.x1, args.y1, args.x2, args.y2)
            return ({"ok": True, "from": [args.x1, args.y1], "to": [args.x2, args.y2]})
        elif cmd == "input":
            dispatched = True
            plat.input_text(args.text)
            return ({"ok": True, "len": len(args.text)})
        elif cmd == "type-send":
            dispatched = True
            plat.tap(args.input_x, args.input_y); time.sleep(0.7)
            if args.replace:
                plat.press_key("ctrl+a")
            plat.input_text(args.text); time.sleep(0.4)
            if not args.prepare_only:
                submission_attempted = True
                plat.tap(args.send_x, args.send_y)
                time.sleep(max(0.0, args.wait_s))
            res = _shot(plat, args.out)
            res.update({"text": args.text, "submitted": not args.prepare_only, "requires_observation": True})
            return (res)
        elif cmd == "key":
            dispatched = True
            plat.press_key(args.key)
            return ({"ok": True, "key": args.key})
        elif cmd == "launch":
            dispatched = True
            plat.reset_app(args.package, "relaunch" if args.force_stop else "none")
            return ({"ok": True, "package": args.package, "force_stop": args.force_stop})
    except Exception as exc:
        error = {"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                 "outcome": "uncertain" if dispatched else "not_dispatched",
                 "requires_observation": dispatched}
        from .diagnostics import collect
        error["diagnostics"] = collect(plat, exc)
        if cmd == "type-send":
            error["submission_attempted"] = submission_attempted
            error["requires_observation"] = dispatched
        return error
    finally:
        if dispatched and plat is not None:
            try:
                if cmd in {"new-page", "select-page", "close-page"}:
                    _page_evidence(plat, serial, args.record, "after")
                else:
                    _record(args.record, "after", _shot(plat, None))
            except Exception as exc:
                _record(args.record, "observation_failed", {"error": str(exc)})
        ds.release_controller(plat)


def _page_evidence(platform, session, record, phase):
    """Never select another page merely to obtain evidence for a closed target."""
    from .observations import capture
    result = {"phase": phase, "selected_page_id": getattr(platform, "page_id", None)}
    try:
        result["pages"] = platform.list_pages()
        result["observation"] = capture(platform, session)
    except Exception as exc:
        result["observation_error"] = f"{type(exc).__name__}: {exc}"
    _record(record, "page_evidence", result)
    return result


def _record(path, kind, data):
    import json
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())


def execute(command, session=None, **options):
    import uuid
    from saygo.platforms import device_session as ds
    from saygo.runtime.locking import resource_guard, session_keys
    record = None
    requested = False
    try:
        keys = session_keys(session)
        with resource_guard(keys):
            if not set(session_keys(session)) <= set(keys):
                raise RuntimeError("Session binding changed while acquiring control; retry after inspecting sessions")
            root = ds.STATE_DIR.parent / "operations"
            root.mkdir(parents=True, exist_ok=True)
            operation_id = uuid.uuid4().hex
            record = root / (operation_id + ".jsonl")
            _record(record, "requested", {"command": command, "session": session or "default", "options": options})
            options["record"] = record
            requested = True
            result = _request(command, session, **options)
            result.update(operation_id=operation_id, record=str(record))
            _record(record, "result", result)
            return result
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                  "outcome": "uncertain" if requested else "not_dispatched", "requires_observation": requested}
        if record:
            result["record"] = str(record)
            try:
                _record(record, "error", result)
            except OSError as journal_error:
                result["journal_error"] = str(journal_error)
        return result
