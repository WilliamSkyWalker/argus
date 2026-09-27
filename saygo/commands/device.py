"""Unified device command arguments, actions and JSON results."""

import json


def _session_options(parser, *, screenshot=False):
    parser.add_argument("--session", "--serial", dest="serial", default=None)
    if screenshot:
        parser.add_argument("--out", default=None)


def register(sub):
    # saygo device <...> — 通用设备驱动原语（常驻 Appium session + 跨进程重连）
    # 任何能跑 shell 的 agent 都能调；每条命令默认输出 JSON 到 stdout。
    dev_p = sub.add_parser("device",
                           help="Control mobile, desktop and browser sessions (JSON output)")
    dev_sub = dev_p.add_subparsers(dest="device_command", required=True)
    from saygo.devices.control import register as register_control
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
    for action_parser in (d_shot, d_tap, d_input, d_ts, d_key, d_swipe):
        action_parser.add_argument("--foreground", action="store_true", help="Explicit Windows foreground input and screen capture for this command")
        action_parser.add_argument("--window-id", help="Explicit eligible window within the bound Windows process")
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
    for name in ("capabilities", "diagnose", "wait", "act"):
        p = dev_sub.add_parser(name)
        _session_options(p, screenshot=name not in {"capabilities", "diagnose"})
        p.add_argument("--foreground", action="store_true", help="Explicit Windows foreground input and screen capture for this command")
        p.add_argument("--window-id", help="Explicit eligible window within the bound Windows process")
        if name == "wait":
            p.add_argument("--mode", choices=["stable", "change"], default="stable")
            p.add_argument("--timeout", type=float, default=5)
        if name == "act":
            p.add_argument("action", type=json.loads, help="JSON action object; type and coordinate_space")
            p.add_argument("--observation-id")
            p.add_argument("--observe-after", action="store_true")
            p.add_argument("--timeout", type=float, default=5)
    d_shot.add_argument("--crop", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    d_tap.add_argument("--coordinate-space", choices=["screen", "percent", "image", "crop"], default="screen")
    d_tap.add_argument("--observation-id")
    d_tap.add_argument("--observe-after", action="store_true")
    d_stop = dev_sub.add_parser("stop", help="Quit the device session")
    _session_options(d_stop)
    dev_p.set_defaults(handler=cmd_device)


def cmd_device(args):
    from saygo.devices import control, service
    command = args.device_command
    if command in {"list", "sessions", "connect", "disconnect", "install", "boot", "network", "handoff", "resume"}:
        return control.dispatch(args)
    options = vars(args).copy()
    for key in ("command", "device_command", "serial", "handler", "verbose"):
        options.pop(key, None)
    result = service.execute(command, getattr(args, "serial", None), **options)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok", True):
        raise SystemExit(2)
