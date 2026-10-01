"""Window capture, native control actions and directed keys; never global input.

This backend does not activate or launch applications. Native controls may reject
actions and apps may ignore directed key events. Verify results with screenshots.
"""
import math
import time

from .desktop_mac import DesktopMacPlatform


class DesktopMacBackgroundPlatform(DesktopMacPlatform):
    # Deliberately exclude inherited global-input methods from the public API.
    ACTIONS = frozenset({"tap", "right_click", "input",
                         "press_key", "scroll_at", "scroll_up", "scroll_down"})
    KEYS = {"enter": 36, "return": 36, "tab": 48, "space": 49,
            "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
            "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
            "left": 123, "right": 124, "down": 125, "up": 126}

    def setup(self, config):
        import Quartz
        import AppKit
        import ApplicationServices
        self._Q, self._AK, self._AX = Quartz, AppKit, ApplicationServices
        self._require_unlocked()
        options = config.get("mac", {})
        self._app_name = str(options.get("app", "")).strip()
        if not self._app_name:
            raise ValueError("macOS background control requires an app name")
        if options.get("foreground"):
            raise ValueError("Background control cannot enable foreground input")
        self._pid = int(options.get("process_id") or 0)
        self._win_id = int(options.get("window_id") or 0)
        window = self._find_window()
        if window is None:
            raise RuntimeError("No eligible background window; open/unminimize the app manually, "
                               "or reconnect if the saved window has closed. No app was activated.")
        self._apply_window(window)

    @property
    def connection(self):
        return {"process_id": self._pid, "window_id": str(self._win_id),
                "background": True, "capture_mode": "background", "input_mode": "native_background"}

    def _windows(self):
        Q = self._Q
        windows = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID)
        return [w for w in windows or []
                if w.get("kCGWindowOwnerName") == self._app_name
                and (not self._pid or int(w["kCGWindowOwnerPID"]) == self._pid)
                and int(w.get("kCGWindowLayer", -1)) in (0, 3)
                and float(w.get("kCGWindowBounds", {}).get("Width", 0)) > 1
                and float(w.get("kCGWindowBounds", {}).get("Height", 0)) > 1]

    def _find_window(self):
        windows = self._windows()
        if self._win_id:
            return next((w for w in windows if int(w["kCGWindowNumber"]) == self._win_id), None)
        if len(windows) > 1:
            raise RuntimeError("Multiple eligible windows; connect with an explicit --window-id")
        return windows[0] if windows else None

    def _ensure_frontmost(self):
        # Used by inherited window capture. Intentionally never activate.
        pass

    def screenshot_raw(self):
        self._require_unlocked()
        check = getattr(self._Q, "CGPreflightScreenCaptureAccess", None)
        if check is not None and not check():
            raise PermissionError("Screen Recording permission is required for the process running Saygo")
        return super().screenshot_raw()

    def observation_metadata(self):
        return {**super().observation_metadata(), "capture_mode": "background",
                "input_mode": "native_background"}

    def _require_unlocked(self):
        session = self._Q.CGSessionCopyCurrentDictionary()
        if not session or not session.get("kCGSSessionOnConsoleKey") or session.get("CGSSessionScreenIsLocked"):
            raise PermissionError("Background control requires an unlocked, active macOS console session")

    def _validate_window(self, expected):
        window = self._find_window()
        if window is None:
            raise RuntimeError("Bound background window disappeared or is minimized; observe again")
        self._apply_window(window)
        current = self.observation_metadata()
        if any(current.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Observed window identity or bounds changed; observe again")

    def _check_window(self):
        self._require_unlocked()
        self._validate_window(getattr(self, "_expected_window", None) or self.observation_metadata())

    def supported_actions(self):
        return self.ACTIONS

    def capability_details(self):
        return {"capture_mode": "background", "input_mode": "native_background",
                "scroll_at_unit": "native_steps", "scroll_value_step": .05,
                "foreground_fallback": False, "requires_current_desktop": True}

    def validate_action(self, action):
        self._require_unlocked()
        kind = action["type"]
        if kind not in self.ACTIONS:
            raise ValueError(f"Unsupported macOS background action: {kind}; foreground fallback is disabled")
        if kind == "press_key" and action["key"].strip().lower() not in self.KEYS:
            raise ValueError("Unsupported background key; modifiers and shortcuts are disabled")
        if kind == "input":
            text = action["text"]
            if len(text) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError("Background input accepts at most 4096 printable characters; use press_key for controls")
            text.encode("utf-16-le")  # Reject malformed surrogate strings before any input.
        if kind == "scroll_at" and (isinstance(action["amount"], bool) or
                                    not isinstance(action["amount"], (int, float)) or
                                    not math.isfinite(action["amount"]) or
                                    not -100 <= action["amount"] <= 100 or
                                    int(action["amount"]) != action["amount"]):
            raise ValueError("Background scroll amount must be an integer in [-100,100]")
        if kind in {"input", "press_key"} and len(self._windows()) != 1:
            raise ValueError("Background keyboard input requires exactly one eligible application window")
        check = getattr(self._Q, "CGPreflightPostEventAccess", None)
        if check is None or not check():
            raise PermissionError("Accessibility permission is required for process-directed input")
        if kind in {"tap", "right_click"}:
            element = self._native_target(action["x"], action["y"])
            native_action = "AXPress" if kind == "tap" else "AXShowMenu"
            error, names = self._AX.AXUIElementCopyActionNames(element, None)
            if error or native_action not in (names or []):
                raise ValueError(f"Target does not support {native_action}; no global-input fallback")
        if kind in {"input", "press_key"}:
            app = self._AX.AXUIElementCreateApplication(self._pid)
            element = self._attribute(app, "AXFocusedUIElement")
            self._check_native_window(element)
        if kind in {"scroll_at", "scroll_up", "scroll_down"}:
            x, y = action.get("x", self._win_w // 2), action.get("y", self._win_h // 2)
            amount = action.get("amount", 5 if kind == "scroll_up" else -5)
            self._native_scrollbar(x, y, amount)

    def _attribute(self, element, name):
        error, value = self._AX.AXUIElementCopyAttributeValue(element, name, None)
        if error or value is None:
            raise ValueError(f"Background target does not expose {name} (AX error {error})")
        return value

    def _check_native_window(self, element):
        AX = self._AX
        error, pid = AX.AXUIElementGetPid(element, None)
        if error or pid != self._pid:
            raise ValueError("Native target belongs to another process")
        window = self._attribute(element, "AXWindow")
        position = self._attribute(window, "AXPosition")
        size = self._attribute(window, "AXSize")
        ok_pos, point = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
        ok_size, extent = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
        if not ok_pos or not ok_size:
            raise ValueError("Cannot establish native target window bounds")
        actual = (point.x, point.y, extent.width, extent.height)
        expected = (self._win_x, self._win_y, self._win_w, self._win_h)
        if any(abs(a - b) > 1 for a, b in zip(actual, expected)):
            raise ValueError("Native target is not in the observed window")
        app = AX.AXUIElementCreateApplication(self._pid)
        windows = self._attribute(app, "AXWindows")
        matches = []
        if len(windows) > 64:
            raise ValueError("Too many native windows to establish an unambiguous target")
        for candidate in windows:
            error_pos, candidate_pos = AX.AXUIElementCopyAttributeValue(candidate, "AXPosition", None)
            error_size, candidate_size = AX.AXUIElementCopyAttributeValue(candidate, "AXSize", None)
            if error_pos or error_size or candidate_pos is None or candidate_size is None:
                continue
            ok_p, p = AX.AXValueGetValue(candidate_pos, AX.kAXValueCGPointType, None)
            ok_s, s = AX.AXValueGetValue(candidate_size, AX.kAXValueCGSizeType, None)
            if ok_p and ok_s and all(abs(a - b) <= 1 for a, b in
                                    zip((p.x, p.y, s.width, s.height), expected)):
                matches.append(candidate)
        if len(matches) != 1 or matches[0] != window:
            raise ValueError("Native window mapping is ambiguous; no input was sent")

    def _native_target(self, x, y):
        # A single app-scoped hit test, not a UI-tree dump or semantic locator.
        AX = self._AX
        app = AX.AXUIElementCreateApplication(self._pid)
        error, element = AX.AXUIElementCopyElementAtPosition(app, *self._to_global(x, y), None)
        if error or element is None:
            raise ValueError(f"No native control at the observed coordinates (AX error {error})")
        self._check_native_window(element)
        return element

    def _native_scrollbar(self, x, y, amount):
        element = self._native_target(x, y)
        # Follow only the hit control's ancestor chain, bounded to its window;
        # never enumerate descendants or return UI structure to the caller.
        for _ in range(12):
            role = self._attribute(element, "AXRole")
            if role == "AXScrollBar":
                scrollbar = element
                break
            if role == "AXScrollArea":
                scrollbar = self._attribute(element, "AXVerticalScrollBar")
                break
            if role in {"AXWindow", "AXApplication"}:
                raise ValueError("No native vertical scroll area at the requested coordinates")
            element = self._attribute(element, "AXParent")
        else:
            raise ValueError("Native scroll ancestor limit reached")
        self._check_native_window(scrollbar)
        action = "AXDecrement" if amount > 0 else "AXIncrement"
        error, names = self._AX.AXUIElementCopyActionNames(scrollbar, None)
        if error or action not in (names or []):
            error, writable = self._AX.AXUIElementIsAttributeSettable(scrollbar, "AXValue", None)
            if error or not writable:
                raise ValueError(f"Native scrollbar supports neither {action} nor a writable AXValue")
            value = self._attribute(scrollbar, "AXValue")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Native scrollbar does not expose a normalized value")
            action = "AXValue"
        return scrollbar, action

    def _context(self):
        front = self._AK.NSWorkspace.sharedWorkspace().frontmostApplication()
        point = self._Q.CGEventGetLocation(self._Q.CGEventCreate(None))
        return (int(front.processIdentifier()) if front else None, float(point.x), float(point.y))

    def _post(self, events):
        self._require_unlocked()
        self._check_window()
        before = self._context()
        # Prepare all events before dispatch. No HID/session posting, cursor warping,
        # activation, clipboard operations or automatic retries are permitted here.
        for event in events:
            if event is None:
                raise RuntimeError("Could not create a background input event")
        for event in events:
            self._Q.CGEventPostToPid(self._pid, event)
            time.sleep(.02)
        time.sleep(.05)
        after = self._context()
        if before[0] != after[0] or math.hypot(after[1] - before[1], after[2] - before[2]) > .5:
            raise RuntimeError("Foreground application or pointer changed during input; result is uncertain. "
                               "Observe before continuing; input was not replayed or focus restored.")

    def _source(self):
        source = self._Q.CGEventSourceCreate(self._Q.kCGEventSourceStatePrivate)
        if source is None:
            raise RuntimeError("Could not create a private background event source")
        return source

    def _target(self, event):
        Q = self._Q
        if event is None:
            raise RuntimeError("Could not create a background input event")
        Q.CGEventSetFlags(event, 0)
        Q.CGEventSetIntegerValueField(event, Q.kCGEventTargetUnixProcessID, self._pid)
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointer, self._win_id)
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, self._win_id)
        return event

    def _click(self, x, y, *, right=False):
        self.validate_action({"type": "right_click" if right else "tap", "x": x, "y": y})
        self._check_window()
        element = self._native_target(x, y)
        before = self._context()
        error = self._AX.AXUIElementPerformAction(element, "AXShowMenu" if right else "AXPress")
        # AXCannotComplete can mean the action happened but its reply timed out.
        # Never retry and never translate a timeout into a definite non-dispatch.
        if error:
            raise RuntimeError(f"Native background action returned AX error {error}; result may be uncertain")
        time.sleep(.05)
        after = self._context()
        if before[0] != after[0] or math.hypot(after[1] - before[1], after[2] - before[2]) > .5:
            raise RuntimeError("Foreground application or pointer changed during native action; result is uncertain")

    def tap(self, x, y):
        self._click(x, y)

    def right_click(self, x, y):
        self._click(x, y, right=True)

    def press_key(self, key):
        self.validate_action({"type": "press_key", "key": key})
        Q = self._Q
        source = self._source()
        events = [self._target(Q.CGEventCreateKeyboardEvent(source, self.KEYS[key.strip().lower()], down))
                  for down in (True, False)]
        self._post(events)

    def input_text(self, text):
        if not text:
            return
        self.validate_action({"type": "input", "text": text})
        Q = self._Q
        source, events = self._source(), []
        # Bound event size, split on code points rather than UTF-16 code units, and
        # never place half of a surrogate pair into an event.
        for start in range(0, len(text), 20):
            chunk = text[start:start + 20]
            units = len(chunk.encode("utf-16-le")) // 2
            for down in (True, False):
                event = self._target(Q.CGEventCreateKeyboardEvent(source, 0, down))
                Q.CGEventKeyboardSetUnicodeString(event, units, chunk)
                events.append(event)
        self._post(events)

    def scroll_at(self, x, y, amount):
        self.validate_action({"type": "scroll_at", "x": x, "y": y, "amount": amount})
        self._check_window()
        element, action = self._native_scrollbar(x, y, amount)
        before = self._context()
        if action == "AXValue":
            value = float(self._attribute(element, "AXValue"))
            error = self._AX.AXUIElementSetAttributeValue(element, "AXValue", max(0., min(1., value - int(amount) * .05)))
            if error:
                raise RuntimeError(f"Native scroll returned AX error {error}; result may be uncertain")
        else:
            for _ in range(abs(int(amount))):
                error = self._AX.AXUIElementPerformAction(element, action)
                if error:
                    raise RuntimeError(f"Native scroll returned AX error {error}; result may be uncertain")
        time.sleep(.05)
        if self._context() != before:
            raise RuntimeError("Foreground application or pointer changed during native scroll; result is uncertain")

    def scroll_up(self):
        self.scroll_at(self._win_w // 2, self._win_h // 2, 5)

    def scroll_down(self):
        self.scroll_at(self._win_w // 2, self._win_h // 2, -5)

    def _unsupported(self, *args, **kwargs):
        raise ValueError("Unsupported macOS background operation; foreground fallback is disabled")

    # Block inherited public and private escape routes, including direct QA calls.
    swipe = hover = double_click = long_press = hotkey = open_target = _open_app = _set_clipboard = _unsupported

    def diagnose(self):
        front, x, y = self._context()
        return {**self.connection, "foreground_process_id": front, "pointer": [x, y],
                "screen_recording": bool(self._Q.CGPreflightScreenCaptureAccess()),
                "post_event_access": bool(self._Q.CGPreflightPostEventAccess()),
                "accessibility_trusted": bool(self._AX.AXIsProcessTrusted()),
                "window_available": self._find_window() is not None,
                "input_delivery": "Application-dependent; verify each result with a fresh screenshot"}

    def get_system_prompt_segment(self):
        return ("macOS background window: use screenshots and window-relative coordinates. "
                "Available: tap (native AXPress), right_click (native AXShowMenu), input (printable Unicode), press_key "
                "(unmodified navigation keys), scroll_at, scroll_up/down. No dragging, shortcuts, "
                "launching, activation or clipboard. Keyboard input needs a focused control in the bound window. "
                "Unsupported native controls fail explicitly. Background events may be ignored by an app; "
                "verify the resulting screenshot and never equate dispatch with success.")
