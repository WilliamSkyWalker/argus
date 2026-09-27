"""Adapters for existing visual drivers and registered read-only SQLite queries."""

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
        if kind in {"mac", "windows"} and "session" in self.spec:
            from ..platforms import device_session
            state = device_session.load_state(self.spec["session"])
            if not state or state.get("kind") != "desktop" or state.get("os") != kind or state.get("disconnected"):
                raise PreconditionError("Desktop session missing, disconnected or platform mismatch")
            extra = {"window_id": self.spec["window_id"]} if "window_id" in self.spec else {}
            self.platform = device_session.attach_desktop(state, serial=self.spec["session"], **extra)
        elif kind in {"android", "ios", "browser"}:
            from ..platforms import device_session
            state = device_session.load_state(self.spec["session"])
            actual = "browser" if state and state.get("kind") == "browser" else (state or {}).get("os")
            if actual != kind or state.get("disconnected"):
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
        from argus.devices.observations import metadata as describe
        metadata = describe(platform, png, self.spec.get("session"))
        if hasattr(platform, "observation_metadata"):
            metadata.update(platform.observation_metadata())
        return png, metadata

    def diagnose(self, error=None):
        from argus.devices.diagnostics import collect
        try:
            return collect(self._attach(), error)
        except Exception as exc:
            return collect(None, error or exc)

    def wait(self, timeout=5):
        from argus.devices.observations import wait
        return wait(self._attach(), "stable", timeout)

    def pages(self):
        platform = self._attach()
        if not hasattr(platform, "list_pages"):
            raise PreconditionError("page management requires Playwright or extension")
        return {"pages": platform.list_pages()}

    def prepare(self, action, observation):
        """Validate all input before the durable dispatch intent is recorded."""
        platform = self._attach()
        from argus.devices import actions
        try:
            if "path" not in observation:
                _, meta = self.observe()
                if any(meta.get(k) != observation.get(k) for k in ("screen_size", "image_sha256", "page_id", "url", "window_id", "process_id", "window_bounds")):
                    raise ValueError("screen changed since observation; capture a fresh observation and replan")
                observation = None
            return actions.prepare(platform, action, observation)
        except ValueError as exc:
            raise PreconditionError(str(exc)) from exc

    def execute(self, action):
        platform = self._attach()
        from argus.devices.actions import dispatch
        result = dispatch(platform, action)
        if hasattr(platform, "page_id"):
            result.update(page_id=platform.page_id, pages=platform.list_pages(),
                          events=platform.drain_events())
        return result

    def close(self):
        if self.platform is None:
            return
        from ..platforms.device_session import release_controller
        release_controller(self.platform, self.spec["kind"])
        self.platform = None


def make_resource(spec):
    return SQLiteResource(spec) if spec["kind"] == "sqlite" else VisualResource(spec)
