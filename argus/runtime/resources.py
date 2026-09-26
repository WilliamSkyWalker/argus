"""Adapters for existing visual drivers and registered read-only SQLite queries."""

import hashlib
import math
from pathlib import Path
import sqlite3
import time


class PreconditionError(RuntimeError):
    """Rejected before sending input to a resource."""


def resource_key(spec):
    kind = spec["kind"]
    if kind in {"mac", "windows"}:
        # All windows share one physical keyboard/mouse, including WSL interop.
        return "desktop:local"
    if kind == "sqlite":
        return "sqlite:" + str(Path(spec["path"]).expanduser().resolve())
    if kind == "browser" and spec.get("backend") == "extension":
        from ..platforms.device_session import load_state
        state = load_state(spec["session"]) or {}
        if "bridge_directory" in state:
            return "browser-extension:" + str(Path(state["bridge_directory"]).resolve())
    # device_session uses one namespace for mobile and browser session files.
    return "device-session:" + spec["session"]


class SQLiteResource:
    def __init__(self, spec):
        self.spec = spec

    def call(self, operation, arguments):
        query = self.spec["queries"][operation]
        path = Path(self.spec["path"]).expanduser().resolve()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            db.row_factory = sqlite3.Row
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                       sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

            def authorize(code, arg1, arg2, database, source):
                if code not in allowed or (code == sqlite3.SQLITE_FUNCTION and
                                          (arg2 or "").lower() in {"load_extension", "writefile", "readfile"}):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(authorize)
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = db.execute(query, arguments).fetchmany(1001)
            if len(rows) > 1000:
                raise ValueError("query exceeds 1000 rows; narrow the registered query")
            return {"rows": [dict(row) for row in rows], "operation": operation}
        finally:
            db.close()

    def close(self):
        pass


class VisualResource:
    def __init__(self, spec):
        self.spec = spec
        self.platform = None

    def _attach(self):
        if self.platform is not None:
            return self.platform
        kind = self.spec["kind"]
        if kind in {"android", "ios", "browser"}:
            from ..platforms import device_session
            state = device_session.load_state(self.spec["session"])
            actual = "browser" if state and state.get("kind") == "browser" else (state or {}).get("os")
            if actual != kind:
                raise PreconditionError("session missing or platform mismatch; start the named device session first")
            if kind == "browser":
                self.platform = device_session.attach_browser(
                    self.spec["session"], backend=self.spec.get("backend", "playwright"),
                    page_id=self.spec.get("page_id"))
            else:
                self.platform = device_session.attach(self.spec["session"], quiet=True)
            if self.platform is None:
                raise PreconditionError("session expired; reconnect it explicitly before resuming")
        else:
            from ..platforms import create_platform
            # Do not inherit unrelated app/launch configuration from the QA .env.
            cfg = {kind: {"app": self.spec["app"]}}
            if kind == "windows":
                cfg = {"win": {"app": self.spec["app"], "launch": ""}}
            self.platform = create_platform(kind, cfg)
            self.platform.setup(cfg)
        return self.platform

    def observe(self):
        platform = self._attach()
        png = platform.screenshot_raw()
        metadata = {"screen_size": list(platform.screen_size),
                    "image_sha256": hashlib.sha256(png).hexdigest()}
        if hasattr(platform, "observation_metadata"):
            metadata.update(platform.observation_metadata())
        return png, metadata

    def pages(self):
        platform = self._attach()
        if not hasattr(platform, "list_pages"):
            raise PreconditionError("page management requires Playwright or extension")
        return {"pages": platform.list_pages()}

    def prepare(self, action, observation):
        """Validate all input before the durable dispatch intent is recorded."""
        platform = self._attach()
        allowed = {"tap", "swipe", "input", "press_key", "scroll_up", "scroll_down", "open_url", "open_app"}
        if self.spec["kind"] == "browser":
            allowed |= {"select_page", "close_page", "new_page", "go_back", "go_forward"}
        kind = action.get("type")
        if kind not in allowed:
            raise PreconditionError(f"unsupported action: {kind}")
        _, meta = self.observe()
        if (meta["screen_size"] != observation["screen_size"] or
                meta["image_sha256"] != observation["image_sha256"] or
                meta.get("page_id") != observation.get("page_id") or
                meta.get("url") != observation.get("url")):
            raise PreconditionError("screen changed since observation; capture a fresh observation and replan")
        w, h = platform.screen_size
        result = {"type": kind}
        if kind in {"tap", "swipe"}:
            pairs = [("x_pct", "x", w), ("y_pct", "y", h)] if kind == "tap" else [
                ("x1_pct", "x1", w), ("y1_pct", "y1", h),
                ("x2_pct", "x2", w), ("y2_pct", "y2", h)]
            for source, dest, size in pairs:
                value = action.get(source)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                    raise PreconditionError(f"{source} must be a finite percentage in [0,100]")
                result[dest] = min(size - 1, int(round(value * size / 100)))
        elif kind == "new_page":
            from urllib.parse import urlsplit
            url = action.get("url")
            if not hasattr(platform, "new_page") or not isinstance(url, str):
                raise PreconditionError("new_page requires a supported browser backend and URL")
            if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
                raise PreconditionError("new_page requires an HTTP(S) URL")
            result["url"] = url
        elif kind in {"select_page", "close_page"}:
            if not hasattr(platform, "list_pages") or not isinstance(action.get("page_id"), str):
                raise PreconditionError("page action requires a supported browser backend and an explicit page_id")
            if action["page_id"] not in {p["page_id"] for p in platform.list_pages()}:
                raise PreconditionError("requested page no longer exists")
            result["page_id"] = action["page_id"]
        else:
            field = {"input": "text", "press_key": "key", "open_url": "url", "open_app": "target"}.get(kind)
            if field:
                if not isinstance(action.get(field), str) or not action[field]:
                    raise PreconditionError(f"{field} must be a nonempty string")
                result[field] = action[field]
        return result

    def execute(self, action):
        platform = self._attach()
        result = {"dispatched": True}
        if action["type"] == "new_page":
            result["created_page_id"] = platform.new_page(action["url"])
        else:
            platform.execute_action(action)
        if hasattr(platform, "page_id"):
            result.update(page_id=platform.page_id, pages=platform.list_pages(),
                          events=platform.drain_events())
        return result

    def close(self):
        if self.platform is None:
            return
        if self.spec["kind"] in {"mac", "windows"}:
            self.platform.teardown()
        elif self.spec["kind"] == "browser":
            # Stop only the temporary ChromeDriver, not the user's persistent Chrome.
            if hasattr(self.platform, "disconnect"):
                self.platform.disconnect()
            else:
                service = getattr(self.platform._driver, "service", None)
                if service:
                    service.stop()
        self.platform = None


def make_resource(spec):
    return SQLiteResource(spec) if spec["kind"] == "sqlite" else VisualResource(spec)
