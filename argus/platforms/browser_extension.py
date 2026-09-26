"""Visual driver for website tabs in a connected browser in an existing browser."""
import base64
import io

from PIL import Image

from argus.integrations.browser_bridge import Client
from .base import Platform
from .browser import BROWSER_PROMPT_SEGMENT
from .browser_playwright import PageSelectionError


class ExtensionBrowserPlatform(Platform):
    def __init__(self):
        self.client = None
        self._page_id = None
        self._selection_callback = None

    def setup(self, config):
        self.connect(config["browser"]["bridge_directory"], config["browser"].get("page_id"))

    def connect(self, directory, page_id=None, selection_callback=None, manage_pages=False):
        self.client = Client(directory)
        self._selection_callback = selection_callback
        self._page_id = page_id
        pages = self.list_pages()
        if page_id is None and len(pages) == 1:
            self._bind(pages[0]["page_id"])
        elif not manage_pages and page_id not in {p["page_id"] for p in pages}:
            raise PageSelectionError("Connect the extension, then explicitly select a live page_id")
        return self

    def _bind(self, page_id):
        if self._selection_callback:
            self._selection_callback(page_id)
        self._page_id = page_id

    @property
    def page_id(self):
        return self._page_id

    def _call(self, operation, **args):
        if self._page_id is None:
            raise PageSelectionError("Explicit page selection required")
        return self.client.call(operation, page_id=self._page_id, **args)

    def list_pages(self):
        return [dict(p, selected=p["page_id"] == self._page_id) for p in self.client.call("pages")]

    def select_page(self, page_id):
        self.client.call("select", page_id=page_id)
        self._bind(page_id)

    def new_page(self, url):
        from urllib.parse import urlsplit
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
            raise ValueError("new_page requires an HTTP(S) URL")
        return self.client.call("new_page", url=url)["page_id"]

    def close_page(self, page_id):
        self.client.call("close", page_id=page_id)

    def observation_metadata(self):
        return dict(self._call("metadata"), pages=self.list_pages())

    def drain_events(self):
        return []

    def screenshot_raw(self):
        result = self._call("screenshot")
        # CDP images may use device pixels. Return CSS pixels to match input.
        raw = base64.b64decode(result["data"], validate=True)
        size = tuple(result["size"])
        with Image.open(io.BytesIO(raw)) as image:
            if image.size == size:
                return raw
            out = io.BytesIO()
            image.resize(size).save(out, format="PNG")
            return out.getvalue()

    def screenshot_png(self):
        return self.screenshot_raw()

    @property
    def screen_size(self):
        return tuple(self._call("size"))

    @property
    def scale(self):
        return 1.0

    def tap(self, x, y):
        self._call("tap", x=x, y=y)

    def input_text(self, text):
        self._call("input", text=text)

    def press_key(self, key):
        self._call("key", key=key)

    def swipe(self, x1, y1, x2, y2):
        self._call("swipe", x1=x1, y1=y1, x2=x2, y2=y2)

    def scroll_up(self):
        self._call("scroll", direction="up")

    def scroll_down(self):
        self._call("scroll", direction="down")

    def open_target(self, target):
        self._call("navigate", url=target)

    def _handle_platform_action(self, action):
        kind = action["type"]
        if kind == "select_page":
            self.select_page(action["page_id"])
        elif kind == "close_page":
            self.close_page(action["page_id"])
        elif kind in {"go_back", "go_forward"}:
            self._call("back" if kind == "go_back" else "forward")
        else:
            raise ValueError(f"Unsupported extension action: {kind}")

    def teardown(self):
        # The extension owns its native host. A short-lived CLI must not stop it.
        self.client = None

    def disconnect(self):
        self.teardown()

    @property
    def platform_name(self):
        return "browser"

    def get_system_prompt_segment(self):
        return BROWSER_PROMPT_SEGMENT
