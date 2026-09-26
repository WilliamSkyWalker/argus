"""Unified device command arguments, actions and JSON results."""

import json
import tempfile as _tempfile
import time
from pathlib import Path


def _session_options(parser, *, screenshot=False):
    parser.add_argument("--session", "--serial", dest="serial", default=None)
    if screenshot:
        parser.add_argument("--out", default=None)


def register(sub):
    # argus device <...> — 通用设备驱动原语（常驻 Appium session + 跨进程重连）
    # 任何能跑 shell 的 agent 都能调；每条命令默认输出 JSON 到 stdout。
    dev_p = sub.add_parser("device",
                           help="Control mobile, desktop and browser sessions (JSON output)")
    dev_sub = dev_p.add_subparsers(dest="device_command", required=True)
    from argus.devices.control import register as register_control
    register_control(dev_sub)
    d_start = dev_sub.add_parser("start", help="Create/reuse a device session")
    _session_options(d_start)
    d_start.add_argument("--os", "--platform", dest="os", default="android",
                         choices=["android", "ios", "browser"],
                         help="android/ios = Appium; browser = persistent Chrome")
    d_start.add_argument("--backend", choices=["selenium", "playwright", "extension"], default=None,
                         help="Browser backend (new device sessions default to selenium; workflows use playwright)")
    d_shot = dev_sub.add_parser("screenshot", help="Screenshot → {path,screen_size,scale}")
    _session_options(d_shot, screenshot=True)
    d_tap = dev_sub.add_parser("tap", help="Tap at device pixel (x, y)")
    d_tap.add_argument("x", type=int)
    d_tap.add_argument("y", type=int)
    _session_options(d_tap)
    d_swipe = dev_sub.add_parser("swipe", help="Swipe (x1,y1)→(x2,y2)")
    for coordinate in ("x1", "y1", "x2", "y2"):
        d_swipe.add_argument(coordinate, type=int)
    d_swipe.add_argument("--duration-ms", type=int, default=300)
    _session_options(d_swipe)
    d_input = dev_sub.add_parser("input", help="Type text into focused field (no submit)")
    d_input.add_argument("text")
    _session_options(d_input)
    d_ts = dev_sub.add_parser("type-send", help="Focus→type→submit→wait→screenshot in one call")
    d_ts.add_argument("text")
    d_ts.add_argument("--input-x", type=int, required=True)
    d_ts.add_argument("--input-y", type=int, required=True)
    d_ts.add_argument("--send-x", type=int)
    d_ts.add_argument("--send-y", type=int)
    d_ts.add_argument("--wait-s", type=float, default=12.0)
    _session_options(d_ts, screenshot=True)
    d_ts.add_argument("--prepare-only", action="store_true", help="Prepare draft and screenshot without submitting")
    d_ts.add_argument("--replace", action="store_true", help="Select all in the input field before typing")
    d_tap.add_argument("--out", default=None, help="Capture result after tapping")
    d_focus = dev_sub.add_parser("focus", help="Restore existing Windows window and capture its visible contents")
    _session_options(d_focus, screenshot=True)
    d_focus.set_defaults(foreground=True)
    d_key = dev_sub.add_parser("key", help="Press key: enter/delete/back/home/recent/…")
    d_key.add_argument("key")
    _session_options(d_key)
    for action_parser in (d_shot, d_tap, d_input, d_ts, d_key):
        action_parser.add_argument("--foreground", action="store_true", help="Explicit Windows foreground input and screen capture for this command")
    d_launch = dev_sub.add_parser("launch", help="Foreground/relaunch a package (Appium activate, no adb)")
    d_launch.add_argument("package")
    _session_options(d_launch)
    d_launch.add_argument("--force-stop", action="store_true", help="terminate then activate (relaunch)")
    d_nav = dev_sub.add_parser("navigate", help="(browser) Open a URL, then screenshot")
    d_nav.add_argument("url")
    d_nav.add_argument("--wait-s", type=float, default=6.0)
    _session_options(d_nav, screenshot=True)
    for page_cmd in ("pages", "select-page", "close-page", "new-page"):
        page_parser = dev_sub.add_parser(page_cmd, help="Browser page management via Playwright or extension")
        page_parser.add_argument("--serial", "--session", required=True)
        if page_cmd == "new-page":
            page_parser.add_argument("url", help="HTTP(S) URL; returns a new page ID without selecting it")
        elif page_cmd != "pages":
            page_parser.add_argument("page_id")
    d_stop = dev_sub.add_parser("stop", help="Quit the device session")
    _session_options(d_stop)
    dev_p.set_defaults(handler=cmd_device)


def cmd_device(args):
    """通用设备驱动 CLI —— 走常驻 Appium session（跨进程按 session_id 重连），
    每条命令输出 JSON 到 stdout，供任何能跑 shell 的 agent 机读驱动设备。"""
    import io as _io
    from argus.platforms import device_session as ds

    cmd = args.device_command
    if cmd in {"list", "sessions", "connect", "disconnect", "install", "boot", "network", "handoff", "resume"}:
        from argus.devices.control import dispatch
        return dispatch(args)
    serial = getattr(args, "serial", None)

    def _out(d):
        print(json.dumps(d, ensure_ascii=False))

    def _shot(plat, out_path):
        png = plat.screenshot_raw()
        if not out_path:
            shots = Path(_tempfile.gettempdir()) / "argus-device-shots"
            shots.mkdir(parents=True, exist_ok=True)
            out_path = str(shots / f"{(serial or 'dev')}-{int(time.time() * 1000)}.png")
        Path(out_path).write_bytes(png)
        from PIL import Image
        with Image.open(_io.BytesIO(png)) as im:
            w, h = im.size
        sw, sh = plat.screen_size
        result = {"path": out_path, "width": w, "height": h,
                  "screen_size": [sw, sh], "scale": plat.scale}
        if hasattr(plat, "observation_metadata"):
            result.update(plat.observation_metadata())
        return result

    plat = None
    submission_attempted = False
    try:
        if cmd == "start":
            os_name = getattr(args, "os", "android")
            plat = ds.start(serial, os_name, browser_backend=getattr(args, "backend", None))
            sw, sh = plat.screen_size
            _out({"ok": True, "serial": serial or "default",
                  "os": getattr(plat, "_os", os_name),
                  "session_id": getattr(getattr(plat, "_driver", None), "session_id", None),
                  "page_id": getattr(plat, "page_id", None), "screen_size": [sw, sh]})
            return
        if cmd == "stop":
            ds.stop(serial)
            _out({"ok": True, "stopped": serial or "default"})
            return

        if cmd in ("pages", "select-page", "close-page", "new-page"):
            state = ds.load_state(serial) or {}
            backend = "extension" if state.get("browser_backend") == "extension" else "playwright"
            plat = ds.attach_browser(serial, backend=backend, manage_pages=True)
            if cmd == "new-page":
                page_id = plat.new_page(args.url)
                _out({"created_page_id": page_id, "selected_page_id": plat.page_id, "pages": plat.list_pages()})
                return
            if cmd == "select-page":
                plat.select_page(args.page_id)
                state = ds.load_state(serial)
                state["browser_backend"] = backend
                ds.save_state(serial, state)
            elif cmd == "close-page":
                plat.close_page(args.page_id)
            _out({"pages": plat.list_pages(), "selected_page_id": plat.page_id})
            return

        # A saved browser must reconnect as-is; never replace it after a selection error.
        state = ds.load_state(serial)
        if state and state.get("disconnected"):
            raise RuntimeError("Session disconnected; use device connect to reconnect")
        foreground = getattr(args, "foreground", False)
        if foreground and (not state or state.get("kind") != "desktop" or state.get("os") != "windows"):
            raise ValueError("--foreground requires a connected Windows desktop session")
        if cmd == "type-send" and not args.prepare_only and (args.send_x is None or args.send_y is None):
            raise ValueError("type-send requires --send-x and --send-y unless --prepare-only is set")
        if state and state.get("kind") == "desktop":
            plat = ds.attach_desktop(state, serial=serial, foreground=foreground)
        elif state and state.get("kind") == "browser":
            plat = ds.attach_browser(serial)
        else:
            plat = ds.attach(serial) or ds.start(serial, "browser" if cmd == "navigate" else "android")

        if cmd == "open":
            plat.open_target(args.target)
            _out({"ok": True, "target": args.target})
        elif cmd == "scroll":
            (plat.scroll_up if args.direction == "up" else plat.scroll_down)()
            _out({"ok": True, "direction": args.direction})
        elif cmd == "navigate":
            plat.open_target(args.url)
            time.sleep(max(0.0, args.wait_s))
            _out(_shot(plat, args.out))
        elif cmd in ("screenshot", "focus"):
            _out(_shot(plat, args.out))
        elif cmd == "tap":
            plat.tap(args.x, args.y)
            result = {"ok": True, "tapped": [args.x, args.y]}
            if args.out:
                time.sleep(.3)
                result.update(_shot(plat, args.out))
                result["requires_observation"] = True
            _out(result)
        elif cmd == "swipe":
            plat.swipe(args.x1, args.y1, args.x2, args.y2)
            _out({"ok": True, "from": [args.x1, args.y1], "to": [args.x2, args.y2]})
        elif cmd == "input":
            plat.input_text(args.text)
            _out({"ok": True, "len": len(args.text)})
        elif cmd == "type-send":
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
            _out(res)
        elif cmd == "key":
            plat.press_key(args.key)
            _out({"ok": True, "key": args.key})
        elif cmd == "launch":
            plat.reset_app(args.package, "relaunch" if args.force_stop else "none")
            _out({"ok": True, "package": args.package, "force_stop": args.force_stop})
    except Exception as exc:
        error = {"ok": False, "error": str(exc)}
        if cmd == "type-send":
            error["submission_attempted"] = submission_attempted
            error["requires_observation"] = submission_attempted
        _out(error)
        raise SystemExit(2) from exc
    finally:
        ds.release_controller(plat)
