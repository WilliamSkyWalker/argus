"""Abstract platform interface — every platform driver must implement this."""

from abc import ABC, abstractmethod


class Platform(ABC):

    # --- Lifecycle ---

    @abstractmethod
    def setup(self, config: dict) -> None:
        """Initialize the platform (boot simulator, launch browser, etc.)."""

    @abstractmethod
    def teardown(self) -> None:
        """Clean up resources."""

    # --- Observation ---

    @abstractmethod
    def screenshot_png(self) -> bytes:
        """Take screenshot with coordinate grid overlay. Returns PNG bytes."""

    def screenshot_raw(self) -> bytes:
        """Take screenshot WITHOUT grid overlay. Returns PNG bytes.

        Default: same as screenshot_png(). Override if the platform
        applies the grid inside screenshot_png().
        """
        return self.screenshot_png()

    @abstractmethod
    def get_ui_tree(self) -> str:
        """Get the UI/DOM tree as a string for LLM consumption."""

    @property
    @abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Logical screen dimensions (width, height)."""

    # --- Actions ---

    @abstractmethod
    def tap(self, x: int, y: int) -> None:
        """Click/tap at logical coordinates."""

    @abstractmethod
    def input_text(self, text: str) -> None:
        """Type text into the currently focused element."""

    @abstractmethod
    def press_key(self, key: str) -> None:
        """Press a keyboard key (enter, delete, tab, escape, etc.)."""

    @abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """Swipe/drag from one point to another."""

    @abstractmethod
    def scroll_up(self) -> None:
        """Scroll up."""

    @abstractmethod
    def scroll_down(self) -> None:
        """Scroll down."""

    @abstractmethod
    def open_target(self, target: str) -> None:
        """Open an app (bundle_id on iOS) or navigate to URL (browser)."""

    def is_ime_visible(self) -> bool:
        """Whether a soft keyboard / IME is currently shown.

        Default False — platforms that can detect this should override.
        Used by keyboard_detector skill to advise the LLM that bottom-of-screen
        elements may be occluded.
        """
        return False

    # --- Platform identity ---

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """e.g. 'ios', 'browser'"""

    @abstractmethod
    def get_system_prompt_segment(self) -> str:
        """Return platform-specific portion of the LLM system prompt."""

    # --- Action dispatch ---

    def execute_action(self, action: dict) -> None:
        """Execute an action dict from LLM output.

        Default implementation handles the common set;
        subclasses override _handle_platform_action for extras.
        """
        action_type = action["type"]
        w, h = self.screen_size

        def clamp(val: int, limit: int) -> int:
            return max(0, min(int(val), limit))

        if action_type == "tap":
            self.tap(clamp(action["x"], w), clamp(action["y"], h))
        elif action_type == "swipe":
            self.swipe(
                clamp(action["x1"], w), clamp(action["y1"], h),
                clamp(action["x2"], w), clamp(action["y2"], h),
            )
        elif action_type in ("swipe_up", "scroll_up"):
            self.scroll_up()
        elif action_type in ("swipe_down", "scroll_down"):
            self.scroll_down()
        elif action_type == "input":
            self.input_text(action["text"])
        elif action_type == "press_key":
            self.press_key(action["key"])
        elif action_type == "wait":
            import time
            wait_time = min(action.get("seconds", 2), 5)
            time.sleep(wait_time)
        elif action_type in ("open_app", "open_url"):
            target = (action.get("bundle_id") or action.get("package")
                      or action.get("url") or action.get("target", ""))
            self.open_target(target)
        else:
            self._handle_platform_action(action)

    def _handle_platform_action(self, action: dict) -> None:
        """Override in subclasses for platform-specific actions."""
        raise ValueError(f"Unknown action type: {action['type']}")
