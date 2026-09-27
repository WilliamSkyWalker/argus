"""Offline backend contracts. Real Chromium tests are in test_playwright_live.py."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from saygo.platforms import device_session
from saygo.platforms.browser_playwright import PlaywrightBrowserPlatform, PageSelectionError
from saygo.runtime.resources import VisualResource, PreconditionError
from saygo.runtime.schema import validate


class PlaywrightTests(TestCase):
    def test_missing_selected_tab_never_falls_back(self):
        platform = PlaywrightBrowserPlatform()
        page = MagicMock()
        page.is_closed.return_value = True
        platform._page = page
        platform._page_id = "closed-target"
        with self.assertRaises(PageSelectionError):
            platform.tap(10, 10)
        page.mouse.click.assert_not_called()

    def test_explicit_selection_is_persisted_before_switch(self):
        platform = PlaywrightBrowserPlatform()
        platform._selection_callback = MagicMock(side_effect=OSError("state unavailable"))
        platform._page_id = "original"
        with self.assertRaises(OSError):
            platform._bind("new", {"new": MagicMock()})
        self.assertEqual(platform.page_id, "original")

    def test_detach_does_not_close_browser_or_context(self):
        platform = PlaywrightBrowserPlatform()
        browser, context, pw = MagicMock(), MagicMock(), MagicMock()
        platform._browser = browser
        platform._contexts = {context}
        platform._playwright = pw
        with patch("saygo.platforms.browser_playwright._release_playwright") as release:
            platform.disconnect()
            release.assert_called_once_with(pw)
        browser.close.assert_not_called()
        context.close.assert_not_called()

    def test_runtime_defaults_to_playwright(self):
        platform = MagicMock()
        with patch.object(device_session, "load_state", return_value={"kind": "browser"}), \
                patch.object(device_session, "attach_browser", return_value=platform) as attach:
            resource = VisualResource({"kind": "browser", "session": "web"})
            self.assertIs(resource._attach(), platform)
            attach.assert_called_once_with("web", backend="playwright", page_id=None)

    def test_runtime_can_keep_selenium(self):
        with patch.object(device_session, "load_state", return_value={"kind": "browser"}), \
                patch.object(device_session, "attach_browser") as attach:
            VisualResource({"kind": "browser", "session": "web", "backend": "selenium"})._attach()
            attach.assert_called_once_with("web", backend="selenium", page_id=None)

    def test_legacy_sessions_still_select_selenium(self):
        state = {"kind": "browser", "debugger_address": "127.0.0.1:9222"}
        with patch.object(device_session, "_selenium_browser_attach", return_value="driver") as attach:
            self.assertEqual(device_session.attach_browser("web", state=state), "driver")
            attach.assert_called_once_with("web", state)

    def test_page_identity_is_checked_even_for_identical_images(self):
        resource = VisualResource({"kind": "browser", "session": "web"})
        resource.platform = MagicMock()
        observation = {"screen_size": [100, 100], "image_sha256": "same", "page_id": "original"}
        with patch.object(resource, "observe", return_value=(b"", dict(observation, page_id="different"))):
            with self.assertRaises(PreconditionError):
                resource.prepare({"type": "tap", "x_pct": 50, "y_pct": 50}, observation)

    def test_navigation_invalidates_observation_even_if_pixels_match(self):
        resource = VisualResource({"kind": "browser", "session": "web"})
        resource.platform = MagicMock()
        observation = {"screen_size": [100, 100], "image_sha256": "same", "page_id": "tab", "url": "https://example.com/first"}
        with patch.object(resource, "observe", return_value=(b"", dict(observation, url="https://example.com/second"))):
            with self.assertRaises(PreconditionError):
                resource.prepare({"type": "tap", "x_pct": 50, "y_pct": 50}, observation)

    def test_unknown_backend_is_rejected_before_start(self):
        with self.assertRaises(ValueError):
            validate({"version": 1, "resources": {"web": {"kind": "browser", "session": "web", "backend": "unknown"}},
                      "steps": [{"id": "screen", "kind": "observe", "resource": "web"}]})

    def test_live_browser_selection_error_does_not_launch_replacement(self):
        state = {"kind": "browser", "port": 9222, "debugger_address": "127.0.0.1:9222"}
        with patch.object(device_session, "load_state", return_value=state), \
                patch.object(device_session, "_devtools_ready", return_value=True), \
                patch.object(device_session, "attach_browser", side_effect=PageSelectionError("missing")), \
                patch("subprocess.Popen") as launch:
            with self.assertRaises(PageSelectionError):
                device_session._browser_start("web")
            launch.assert_not_called()
