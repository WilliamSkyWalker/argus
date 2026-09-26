"""Visual Chromium driver attached over CDP; never owns the browser process.

Page identity is the Chromium target ID, stable across controller reconnects.
Only explicit page selection changes the target; popups never steal it.
"""

import threading

from .base import Platform
from .browser import BROWSER_PROMPT_SEGMENT


_local = threading.local()


def _acquire_playwright():
    # Sync Playwright owns a thread-local event loop. Multiple resources in one
    # workflow must share that loop instead of nesting sync_playwright().start().
    if not getattr(_local, "instance", None):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed; pip install 'playwright>=1.60,<2'") from exc
        _local.instance = sync_playwright().start()
        _local.users = 0
    _local.users += 1
    return _local.instance


def _release_playwright(instance):
    if getattr(_local, "instance", None) is not instance:
        raise RuntimeError("Playwright connections must be released on their owning thread")
    _local.users -= 1
    if _local.users == 0:
        _local.instance = None
        instance.stop()


class PageSelectionError(RuntimeError):
    pass


class PlaywrightBrowserPlatform(Platform):
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._page_id = None
        self._selection_callback = None
        self._contexts = set()
        self._events = []

    def setup(self, config):
        browser = config.get("browser", {})
        endpoint = browser.get("cdp_endpoint")
        if not endpoint:
            raise ValueError("Playwright requires an existing Chromium cdp_endpoint")
        self.connect(endpoint, page_id=browser.get("page_id"))

    def connect(self, endpoint, page_id=None, selection_callback=None, manage_pages=False):
        if self._playwright is not None:
            raise RuntimeError("Browser controller is already connected")
        self._selection_callback = selection_callback
        self._playwright = _acquire_playwright()
        try:
            endpoint = endpoint if "://" in endpoint else "http://" + endpoint
            self._browser = self._playwright.chromium.connect_over_cdp(
                endpoint, timeout=15000, no_defaults=True)
            self._watch_contexts()
            pages = self._page_map()
            if page_id is not None:
                self._page_id = page_id
                self._page = pages.get(page_id)
                if self._page is None and not manage_pages:
                    raise PageSelectionError(f"Selected page {page_id} is closed or missing; list pages and explicitly select one")
            elif len(pages) == 1:
                self._bind(next(iter(pages)), pages)
            elif not manage_pages:
                raise PageSelectionError("No selected page and browser has zero or multiple pages; list pages and explicitly select one")
            return self
        except BaseException:
            self.teardown()
            raise

    def _watch_contexts(self):
        for context in self._browser.contexts:
            if context in self._contexts:
                continue
            self._contexts.add(context)
            context.set_default_timeout(15000)
            context.set_default_navigation_timeout(15000)
            context.on("page", self._on_page)
            # Do not silently dismiss native JS dialogs while the user is taking over.
            context.on("dialog", self._on_dialog)

    def _on_page(self, page):
        self._events.append(("page_opened", page))

    def _on_dialog(self, dialog):
        self._events.append(("dialog", dialog))

    @staticmethod
    def _target_info(page):
        session = page.context.new_cdp_session(page)
        try:
            return session.send("Target.getTargetInfo")["targetInfo"]
        finally:
            session.detach()

    def _page_map(self):
        self._watch_contexts()
        pages = {}
        for context in self._browser.contexts:
            for page in context.pages:
                if page.is_closed():
                    continue
                try:
                    info = self._target_info(page)
                except Exception:
                    if page.is_closed():
                        continue
                    raise
                pages[info["targetId"]] = page
        return pages

    def _bind(self, page_id, pages=None):
        pages = self._page_map() if pages is None else pages
        if page_id not in pages:
            raise PageSelectionError(f"Page {page_id} does not exist")
        if self._selection_callback:
            # Commit selection before changing the controller's target.
            self._selection_callback(page_id)
        self._page_id, self._page = page_id, pages[page_id]

    @property
    def page_id(self):
        return self._page_id

    @property
    def page(self):
        if self._page is None or self._page.is_closed():
            raise PageSelectionError("Selected page is closed or missing; explicitly select a live page")
        return self._page

    def list_pages(self):
        rows = []
        for page_id, page in self._page_map().items():
            try:
                info = self._target_info(page)
                rows.append({"page_id": page_id, "url": info.get("url", page.url),
                             "title": info.get("title", ""), "opener_id": info.get("openerId"),
                             "selected": page_id == self._page_id})
            except Exception:
                if not page.is_closed():
                    raise
        return rows

    def select_page(self, page_id):
        self._bind(page_id)
        self.page.bring_to_front()

    def new_page(self, url):
        from urllib.parse import urlsplit
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
            raise ValueError("new_page requires an HTTP(S) URL")
        if self._page is not None and not self._page.is_closed():
            context = self._page.context
        elif len(self._browser.contexts) == 1:
            context = self._browser.contexts[0]
        else:
            raise PageSelectionError("Select a page to identify the browser context first")
        page = context.new_page()
        # Keep a created tab on navigation failure; do not retry creation blindly.
        page.goto(url, wait_until="domcontentloaded")
        return self._target_info(page)["targetId"]

    def close_page(self, page_id):
        pages = self._page_map()
        if page_id not in pages:
            raise PageSelectionError(f"Page {page_id} does not exist")
        pages[page_id].close(run_before_unload=False)
        # Keep the closed selection ID: the next connection must not pick another tab.
        if page_id == self._page_id:
            self._page = None

    def observation_metadata(self):
        page = self.page
        return {"page_id": self._page_id, "url": page.url, "pages": self.list_pages()}

    def drain_events(self):
        # Poll the protocol so pending context events are delivered.
        self._page_map()
        events, self._events = self._events, []
        result = []
        for kind, obj in events:
            if kind == "page_opened":
                if not obj.is_closed():
                    result.append({"kind": kind, "page_id": self._target_info(obj)["targetId"], "url": obj.url})
            else:
                result.append({"kind": kind, "type": obj.type, "message": obj.message})
        return result

    def teardown(self):
        # browser.close()/context.close() are deliberately not used on an owned-by-user session.
        playwright, self._playwright = self._playwright, None
        self._browser = self._page = None
        for context in self._contexts:
            context.remove_listener("page", self._on_page)
            context.remove_listener("dialog", self._on_dialog)
        self._contexts.clear()
        self._events.clear()
        if playwright is not None:
            _release_playwright(playwright)

    def disconnect(self):
        self.teardown()

    def screenshot_raw(self):
        # CDP no_defaults avoids overriding the user's context. In that mode a
        # background tab can have suspended animation frames; activate our pinned
        # page before capturing it (also makes the handoff screen unambiguous).
        page = self.page
        page.bring_to_front()
        # CSS pixels align screenshots with mouse coordinates, including HiDPI screens.
        return page.screenshot(type="png", full_page=False, scale="css", timeout=15000)

    def screenshot_png(self):
        return self.screenshot_raw()

    @property
    def screen_size(self):
        return tuple(self.page.evaluate("[window.innerWidth, window.innerHeight]"))

    @property
    def scale(self):
        return 1.0

    def tap(self, x, y):
        self.page.mouse.click(x, y)

    def input_text(self, text):
        self.page.keyboard.insert_text(text)

    def press_key(self, key):
        mapping = {"enter": "Enter", "delete": "Backspace", "tab": "Tab", "space": "Space",
                   "escape": "Escape", "select_all": "ControlOrMeta+A", "backspace": "Backspace",
                   "arrow_up": "ArrowUp", "arrow_down": "ArrowDown"}
        self.page.keyboard.press(mapping.get(key, key))

    def swipe(self, x1, y1, x2, y2):
        mouse = self.page.mouse
        mouse.move(x1, y1)
        mouse.down()
        try:
            mouse.move(x2, y2, steps=10)
        finally:
            mouse.up()

    def scroll_up(self):
        self.page.mouse.wheel(0, -300)

    def scroll_down(self):
        self.page.mouse.wheel(0, 300)

    def open_target(self, target):
        self.page.goto(target, wait_until="domcontentloaded")

    def _handle_platform_action(self, action):
        kind = action["type"]
        if kind == "select_page":
            self.select_page(action["page_id"])
        elif kind == "close_page":
            self.close_page(action["page_id"])
        elif kind == "go_back":
            self.page.go_back(wait_until="domcontentloaded")
        elif kind == "go_forward":
            self.page.go_forward(wait_until="domcontentloaded")
        elif kind == "hover":
            self.page.mouse.move(action["x"], action["y"])
        else:
            raise ValueError(f"Unknown action type for Playwright: {kind}")

    @property
    def platform_name(self):
        return "browser"

    def get_system_prompt_segment(self):
        return BROWSER_PROMPT_SEGMENT
