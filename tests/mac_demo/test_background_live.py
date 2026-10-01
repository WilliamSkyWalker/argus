"""Explicitly enabled local GUI tests; never activate or operate user documents."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from saygo.devices import actions, observations
from saygo.platforms import device_session as ds
from saygo.platforms.desktop_mac_background import DesktopMacBackgroundPlatform


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("SAYGO_TEST_MAC_BACKGROUND") == "1",
                     "Opt-in: SAYGO_TEST_MAC_BACKGROUND=1 on an unlocked macOS desktop")
class MacBackgroundLiveTests(unittest.TestCase):
    def setUp(self):
        import Quartz as Q
        import AppKit as A
        self.Q, self.A = Q, A
        session = Q.CGSessionCopyCurrentDictionary() or {}
        if session.get("CGSSessionScreenIsLocked") or not session.get("kCGSSessionOnConsoleKey"):
            self.skipTest("Unlock the active console before running GUI validation")
        if not Q.CGPreflightScreenCaptureAccess() or not Q.CGPreflightPostEventAccess():
            self.skipTest("Screen Recording and Accessibility permissions are required")
        initial_pointer = Q.CGEventGetLocation(Q.CGEventCreate(None))
        initial_front = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        initial_context = (int(initial_front.processIdentifier()) if initial_front else None,
                           float(initial_pointer.x), float(initial_pointer.y))
        self.clipboard_count = A.NSPasteboard.generalPasteboard().changeCount()
        self.temporary = tempfile.TemporaryDirectory(prefix="saygo-mac-background-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        directory = patch.object(ds, "STATE_DIR", self.root / "device-sessions")
        directory.start()
        self.addCleanup(directory.stop)
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("background_fixture.py")), str(self.root)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_fixture)
        deadline = time.monotonic() + 10
        while not (self.root / "ready.json").exists():
            if self.proc.poll() is not None:
                self.fail("Fixture failed to start: " + self.proc.stderr.read())
            if time.monotonic() >= deadline:
                self.fail("Fixture did not initialize within 10 seconds")
            time.sleep(.05)
        info = json.loads((self.root / "ready.json").read_text())
        while True:
            visible = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, 0)
            target = next((w for w in visible if int(w["kCGWindowNumber"]) == info["window_id"]), None)
            if target is not None:
                info["app"] = str(target["kCGWindowOwnerName"])
                break
            if time.monotonic() >= deadline:
                self.fail("Fixture window is not on the current desktop")
            time.sleep(.05)
        self.platform = DesktopMacBackgroundPlatform()
        self.addCleanup(self.platform.teardown)
        self.platform.setup({"mac": {"app": info["app"], "process_id": info["pid"],
                                     "window_id": info["window_id"], "background": True}})
        stable = observations.wait(self.platform, "stable", 3)
        self.assertTrue(stable["condition_met"], "Fixture did not settle before testing")
        self.baseline = self.platform._context()
        self.assertEqual(self.baseline, initial_context, "Fixture setup or attachment changed foreground/pointer")
        self.assertEqual(A.NSPasteboard.generalPasteboard().changeCount(), self.clipboard_count)
        self.assertNotEqual(self.baseline[0], self.proc.pid, "Fixture must remain a background app")

    def stop_fixture(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc.stderr.close()

    def state(self):
        return json.loads((self.root / "fixture-state.json").read_text())

    def act(self, action):
        before = observations.capture(self.platform, "fixture")
        prepared = actions.prepare(self.platform, action, before)
        actions.dispatch(self.platform, prepared)
        time.sleep(.1)
        after = observations.capture(self.platform, "fixture")
        self.assertEqual(self.platform._context(), self.baseline, "Frontmost application or pointer changed")
        self.assertEqual(self.A.NSPasteboard.generalPasteboard().changeCount(), self.clipboard_count,
                         "Background input changed the system clipboard")
        return before, after

    def test_native_button(self):
        _, after = self.act({"type": "tap", "x": 110, "y": 105})
        self.assertEqual(self.state()["clicks"], 1)
        self.assertEqual(after["capture_mode"], "background")

    def test_cli_reconnect_and_observed_action(self):
        env = {**os.environ, "SAYGO_HOME_DIR": str(self.root / "cli-state")}

        def cli(*arguments):
            result = subprocess.run([sys.executable, "-m", "saygo.cli", "device", *arguments],
                                    capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        connection = cli("connect", "--platform", "mac", "--app", self.platform._app_name,
                         "--background", "--window-id", str(self.platform._win_id), "--session", "fixture")
        self.assertTrue(connection["connected"])
        shot = cli("screenshot", "--session", "fixture")
        self.assertEqual(shot["capture_mode"], "background")
        self.assertEqual(shot["window_id"], str(self.platform._win_id))
        result = cli("act", json.dumps({"type": "tap", "x": 110, "y": 105}),
                     "--session", "fixture", "--observation-id", shot["id"], "--observe-after")
        self.assertTrue(result["dispatched"])
        self.assertIsNone(result["business_success"])
        self.assertEqual(self.state()["clicks"], 1)
        self.assertEqual(self.platform._context(), self.baseline)
        self.assertEqual(self.A.NSPasteboard.generalPasteboard().changeCount(), self.clipboard_count)

    def test_unicode_and_delete(self):
        self.act({"type": "input", "text": "Example 中文🙂x"})
        self.assertEqual(self.state()["text"], "Example 中文🙂x")
        self.act({"type": "press_key", "key": "delete"})
        self.assertEqual(self.state()["text"], "Example 中文🙂")

    def test_mcp_background_session_over_stdio(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise():
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "saygo.mcp.server", "--profile", "device"],
                env={**os.environ, "SAYGO_HOME_DIR": str(self.root / "mcp-state")})
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()

                    async def call(name, arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(getattr(result, "is_error", getattr(result, "isError", False)), str(result))
                        value = json.loads(next(c.text for c in result.content if c.type == "text"))
                        self.assertNotEqual(value.get("ok"), False, value)
                        return value, result

                    connected, _ = await call("device_connect", {"platform": "mac", "session": "fixture",
                        "options": {"app": self.platform._app_name, "background": True,
                                    "window_id": str(self.platform._win_id)}})
                    self.assertTrue(connected["connected"])
                    capabilities, _ = await call("device_command", {"command": "capabilities", "session": "fixture"})
                    self.assertFalse(capabilities["foreground_fallback"])
                    self.assertNotIn("hotkey", capabilities["actions"])
                    shot, image = await call("device_observe", {"session": "fixture"})
                    self.assertTrue(any(c.type == "image" for c in image.content))
                    acted, image = await call("device_act", {"session": "fixture", "observation_id": shot["id"],
                        "action": {"type": "tap", "x": 110, "y": 105}, "observe_after": True})
                    self.assertTrue(acted["dispatched"])
                    self.assertIsNone(acted["business_success"])
                    self.assertTrue(any(c.type == "image" for c in image.content))
                    self.assertEqual(self.state()["clicks"], 1)

        asyncio.run(asyncio.wait_for(exercise(), timeout=30))
        self.assertEqual(self.platform._context(), self.baseline)
        self.assertEqual(self.A.NSPasteboard.generalPasteboard().changeCount(), self.clipboard_count)

    def test_directed_scroll(self):
        self.act({"type": "scroll_at", "x": 200, "y": 260, "amount": -5})
        self.assertGreater(self.state()["scrolls"], 0)


if __name__ == "__main__":
    unittest.main()
