"""Offline background contracts: no global input, fallback, or target replacement."""
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from saygo.devices import actions, control
from saygo.platforms import create_platform, device_session as ds
from saygo.platforms.desktop_mac_background import DesktopMacBackgroundPlatform


def window(number=11, pid=22, x=100, app="Example"):
    return {"kCGWindowNumber": number, "kCGWindowOwnerPID": pid,
            "kCGWindowOwnerName": app, "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": x, "Y": 50, "Width": 400, "Height": 300}}


class Quartz:
    """Only process-directed event APIs exist on this fake."""
    kCGWindowListOptionOnScreenOnly = 1
    kCGNullWindowID = 0
    kCGEventSourceStatePrivate = -1
    kCGEventTargetUnixProcessID = 40
    kCGMouseEventWindowUnderMousePointer = 91
    kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent = 92
    kCGMouseEventClickState = 1
    kCGEventLeftMouseDown, kCGEventLeftMouseUp = 1, 2
    kCGEventRightMouseDown, kCGEventRightMouseUp = 3, 4
    kCGMouseButtonLeft, kCGMouseButtonRight = 0, 1
    kCGScrollEventUnitLine = 1

    def __init__(self):
        self.windows, self.posted = [window()], []
        self.allowed = True
        self.locked = False

    def CGSessionCopyCurrentDictionary(self):
        return {"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": self.locked}

    def CGWindowListCopyWindowInfo(self, option, unused):
        assert option == self.kCGWindowListOptionOnScreenOnly
        return self.windows

    def CGPreflightPostEventAccess(self):
        return self.allowed

    def CGEventSourceCreate(self, state):
        assert state == -1
        return "private"

    def CGEventCreateMouseEvent(self, source, kind, point, button):
        return {"kind": kind, "point": point, "button": button}

    def CGEventCreateKeyboardEvent(self, source, key, down):
        return {"key": key, "down": down}

    def CGEventCreateScrollWheelEvent(self, source, unit, axes, amount):
        return {"scroll": amount}

    def CGEventSetLocation(self, event, point):
        event["point"] = point

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventSetIntegerValueField(self, event, field, value):
        event[field] = value

    def CGEventKeyboardSetUnicodeString(self, event, units, text):
        assert units == len(text.encode("utf-16-le")) // 2
        event["text"] = text

    def CGEventPostToPid(self, pid, event):
        self.posted.append((pid, event))


class Accessibility:
    kAXValueCGPointType, kAXValueCGSizeType = 1, 2

    def __init__(self):
        self.actions = []
        self.names = ["AXPress", "AXShowMenu"]
        self.error = 0
        self.pid = 22
        self.position = SimpleNamespace(x=100, y=50)
        self.value = .2
        self.writable = True
        self.writes = []

    def AXUIElementCreateApplication(self, pid):
        return "app"

    def AXUIElementCopyElementAtPosition(self, app, x, y, out):
        self.point = (x, y)
        return 0, "control"

    def AXUIElementCopyActionNames(self, element, out):
        return 0, self.names

    def AXUIElementCopyAttributeValue(self, element, name, out):
        if name == "AXRole":
            return 0, {"control": "AXTextArea", "area": "AXScrollArea", "scrollbar": "AXScrollBar"}[element]
        return 0, {"AXFocusedUIElement": "control", "AXWindow": "window",
                   "AXWindows": ["window"],
                   "AXParent": "area", "AXVerticalScrollBar": "scrollbar", "AXValue": self.value,
                   "AXPosition": self.position,
                   "AXSize": SimpleNamespace(width=400, height=300)}[name]

    def AXUIElementGetPid(self, element, out):
        return 0, self.pid

    def AXValueGetValue(self, value, kind, out):
        return True, value

    def AXUIElementPerformAction(self, element, action):
        self.actions.append((element, action))
        return self.error

    def AXUIElementIsAttributeSettable(self, element, name, out):
        return 0, self.writable

    def AXUIElementSetAttributeValue(self, element, name, value):
        self.writes.append((element, name, value))
        return self.error


class MacBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.p = DesktopMacBackgroundPlatform()
        self.p._Q = Quartz()
        self.p._AX = Accessibility()
        self.p._app_name = "Example"
        self.p._apply_window(window())
        self.p.expect_window(self.p.observation_metadata())
        self.p._context = Mock(return_value=(99, 800., 600.))
        self.p._pg = Mock()
        sleeper = patch("saygo.platforms.desktop_mac_background.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        self.assertEqual(self.p._pg.mock_calls, [])

    def test_factory_background_is_explicit(self):
        self.assertIsInstance(create_platform("mac", {"mac": {"background": True}}), type(self.p))
        self.assertNotIsInstance(create_platform("mac", {}), type(self.p))

    def test_native_click_targets_control_without_global_input(self):
        self.p.tap(20, 30)
        self.assertEqual(self.p._AX.point, (120, 80))
        self.assertEqual(self.p._AX.actions, [("control", "AXPress")])
        self.assertEqual(self.p._Q.posted, [])

    def test_native_rejection_and_wrong_window_never_fall_back(self):
        self.p._AX.names = []
        with self.assertRaisesRegex(ValueError, "does not support"):
            self.p.tap(20, 30)
        self.p._AX.names = ["AXPress"]
        self.p._AX.pid = 999
        with self.assertRaisesRegex(ValueError, "another process"):
            self.p.tap(20, 30)
        self.p._AX.pid = 22
        self.p._AX.position.x = 1010
        with self.assertRaisesRegex(ValueError, "not in the observed window"):
            self.p.tap(20, 30)
        self.assertEqual(self.p._AX.actions, [])
        self.assertEqual(self.p._Q.posted, [])

    def test_native_timeout_is_not_replayed(self):
        self.p._AX.error = -25204
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.p.tap(20, 30)
        self.assertEqual(len(self.p._AX.actions), 1)

    def test_locked_console_rejects_input(self):
        self.p._Q.locked = True
        with self.assertRaisesRegex(PermissionError, "unlocked"):
            self.p.tap(20, 30)
        self.assertEqual(self.p._AX.actions, [])
        self.assertEqual(self.p._Q.posted, [])

    def test_scroll_uses_native_control_and_reports_units(self):
        self.p.scroll_at(100, 100, -3)
        self.assertEqual(len(self.p._AX.writes), 1)
        self.assertEqual(self.p._AX.writes[0][:2], ("scrollbar", "AXValue"))
        self.assertAlmostEqual(self.p._AX.writes[0][2], .35)
        self.assertEqual(self.p._Q.posted, [])
        self.assertEqual(actions.capabilities(self.p)["scroll_at_unit"], "native_steps")
        self.p._AX.writable = False
        with self.assertRaisesRegex(ValueError, "neither"):
            self.p.scroll_at(100, 100, -1)
        self.assertEqual(len(self.p._AX.writes), 1)

    def test_scroll_actions_and_fractional_amount_rejection(self):
        self.p._AX.names.append("AXIncrement")
        self.p.scroll_at(100, 100, -2)
        self.assertEqual(self.p._AX.actions, [("scrollbar", "AXIncrement")] * 2)
        with self.assertRaises(ValueError):
            actions.prepare(self.p, {"type": "scroll_at", "x": 100, "y": 100, "amount": .5})
        self.assertEqual(len(self.p._AX.actions), 2)

    def test_setup_does_not_import_or_call_global_input_or_launch_app(self):
        import sys
        appkit = Mock()
        with patch.dict(sys.modules, {"Quartz": self.p._Q, "AppKit": appkit,
                                      "ApplicationServices": self.p._AX, "pyautogui": None}):
            self.p.setup({"mac": {"app": "Example", "background": True}})
        self.assertEqual(appkit.mock_calls, [])

    def test_cli_accepts_explicit_mac_background_window(self):
        from saygo.cli import build_parser
        options = build_parser().parse_args(["device", "connect", "--platform", "mac", "--app", "Example",
                                            "--session", "test", "--background", "--window-id", "11"])
        self.assertTrue(options.background)
        self.assertEqual(options.window_id, "11")

    def test_unicode_is_complete_and_never_pasted(self):
        with patch.object(self.p, "_set_clipboard") as clipboard:
            text = "Example 中文🙂" * 3
            self.p.input_text(text)
            down = [e["text"] for _, e in self.p._Q.posted if e["down"]]
            self.assertEqual("".join(down), text)
            for pid, event in self.p._Q.posted:
                self.assertEqual((pid, event[40], event[91], event[92]), (22, 22, 11, 11))
                self.assertEqual(event["flags"], 0)
            clipboard.assert_not_called()

    def test_keyboard_checks_permissions_and_single_window_before_post(self):
        self.p._Q.allowed = False
        with self.assertRaises(PermissionError):
            self.p.press_key("enter")
        self.p._Q.allowed = True
        self.p._Q.windows.append(window(number=12))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.p.input_text("text")
        self.assertEqual(self.p._Q.posted, [])

    def test_disallowed_actions_have_no_foreground_fallback(self):
        for method, args in [("swipe", (0, 0, 20, 20)), ("hover", (20, 20)),
                             ("hotkey", (["command", "q"],)), ("long_press", (20, 20)),
                             ("open_target", ("Example",)), ("_open_app", ("Example",)),
                             ("_set_clipboard", ("text",))]:
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "fallback is disabled"):
                getattr(self.p, method)(*args)
        self.assertEqual(self.p._Q.posted, [])
        self.assertNotIn("swipe", actions.capabilities(self.p)["actions"])
        with self.assertRaises(ValueError):
            actions.prepare(self.p, {"type": "hotkey", "keys": ["command", "q"]})

    def test_invalid_keys_or_text_fail_before_input(self):
        for action in [{"type": "press_key", "key": "command+q"},
                       {"type": "input", "text": "text\n"},
                       {"type": "input", "text": "\ud800"}]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                actions.prepare(self.p, action)
        self.assertEqual(self.p._Q.posted, [])

    def test_target_replacement_and_movement_are_rejected(self):
        for replacement in (window(number=12), window(pid=23), window(x=101)):
            self.p._Q.windows = [replacement]
            with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                self.p.tap(20, 20)
        self.assertEqual(self.p._Q.posted, [])

    def test_pointer_or_focus_change_reports_uncertainty_without_retry(self):
        self.p._context.side_effect = [(99, 800., 600.), (100, 800., 600.)]
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.p.tap(20, 20)
        self.assertEqual(len(self.p._AX.actions), 1)

    def test_background_capture_does_not_activate(self):
        self.p._AK = Mock()
        self.p._ensure_frontmost()
        self.assertEqual(self.p._AK.mock_calls, [])

    def test_ambiguous_windows_require_explicit_selection(self):
        self.p._win_id = 0
        self.p._Q.windows.append(window(number=12))
        with self.assertRaisesRegex(RuntimeError, "--window-id"):
            self.p._find_window()

    def test_background_binding_survives_new_controller(self):
        with tempfile.TemporaryDirectory() as root, patch.object(ds, "STATE_DIR", Path(root)):
            args = Namespace(session="test", platform="mac", app="Example", background=True)
            with patch.object(ds, "attach_desktop", return_value=self.p), patch.object(self.p, "teardown"):
                result = control.connect(args)
            self.assertTrue(result["connected"])
            state = ds.load_state("test")
            self.assertEqual((state["background"], state["process_id"], state["window_id"]), (True, 22, "11"))
            with patch("saygo.platforms.create_platform") as factory:
                ds.attach_desktop(state, serial="test")
                self.assertEqual(factory.call_args.args[1]["mac"]["window_id"], "11")
                self.assertTrue(factory.call_args.args[1]["mac"]["background"])


if __name__ == "__main__":
    unittest.main()
