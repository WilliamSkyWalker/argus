"""PC Browser platform — uses Selenium for screenshot + coordinate-based control.

Supports both local browser and remote Selenium Grid.
"""

import io
import platform

from PIL import Image

from ..grid import draw_coordinate_grid, img_to_png_bytes
from ..logger import get_logger
from .base import Platform

log = get_logger("browser")

BROWSER_PROMPT_SEGMENT = """你正在操作一个 PC 端浏览器来执行测试用例。

可用操作类型：
- tap: {"x": int, "y": int}  — 点击页面坐标
- swipe: {"x1": int, "y1": int, "x2": int, "y2": int}  — 拖拽
- scroll_up  — 向上滚动页面
- scroll_down  — 向下滚动页面
- input: {"text": "string"}  — 输入文字（在当前焦点元素）
- press_key: {"key": "enter|delete|tab|space|escape"}  — 按键
- open_url: {"url": "https://..."}  — 导航到URL
- go_back  — 浏览器后退
- go_forward  — 浏览器前进
- hover: {"x": int, "y": int}  — 鼠标悬停（触发下拉菜单等）
- wait: {"seconds": int}  — 等待页面加载（1-5秒）
- done: {"result": "pass|fail", "reason": "string"}  — 报告测试结果

注意：
- 这是纯视觉模式，截图无任何辅助标记，请使用视觉定位能力直接给出坐标
- 网页加载需要时间，如果看到空白页面或加载中状态，使用 wait 等待
- 表单输入：先 tap 输入框使其获得焦点，再用 input 输入文字
- 需要清除输入框已有内容时，可先全选 (press_key "select_all") 再输入新内容
- 坐标相对于浏览器视口，左上角为 (0,0)"""

_IS_MAC = platform.system() == "Darwin"


class BrowserPlatform(Platform):
    """PC Browser platform using Selenium. Supports local browser and Selenium Grid."""

    def __init__(self):
        self._driver = None
        self._viewport_width = 1280
        self._viewport_height = 720
        self._is_remote = False

    def setup(self, config: dict) -> None:
        try:
            from selenium import webdriver
        except ImportError:
            raise RuntimeError(
                "Selenium is not installed. Run:\n"
                "  pip install selenium"
            )

        browser_cfg = config.get("browser", {})
        browser_type = browser_cfg.get("type", "chrome")
        headless = browser_cfg.get("headless", False)
        self._viewport_width = browser_cfg.get("viewport_width", 1280)
        self._viewport_height = browser_cfg.get("viewport_height", 720)
        start_url = browser_cfg.get("start_url", "")
        grid_url = browser_cfg.get("grid_url", "")

        if grid_url:
            # Clean up stale sessions unless caller already did it
            if not browser_cfg.get("_skip_grid_cleanup"):
                self._cleanup_grid_sessions(grid_url)
            # Remote — Selenium Grid / Hub
            self._driver = self._create_remote_driver(webdriver, browser_type,
                                                       headless, grid_url)
            self._is_remote = True
            log.info("已连接 Selenium Grid: %s (browser=%s)", grid_url, browser_type)
        else:
            # Local browser
            self._driver = self._create_local_driver(webdriver, browser_type, headless)
            log.info("本地浏览器已启动: %s (headless=%s)", browser_type, headless)

        self._driver.set_window_size(self._viewport_width, self._viewport_height)

        # Chrome's chrome (address bar etc) eats some viewport pixels — detect
        # the actual usable viewport so coordinates we hand to the LLM match
        # what's really rendered.
        try:
            actual = self._driver.execute_script(
                "return [window.innerWidth, window.innerHeight];"
            )
            if actual and len(actual) == 2 and actual[0] > 0 and actual[1] > 0:
                if (actual[0], actual[1]) != (self._viewport_width, self._viewport_height):
                    log.info("视口校正: 配置 %dx%d → 实际 %dx%d",
                             self._viewport_width, self._viewport_height,
                             actual[0], actual[1])
                self._viewport_width = actual[0]
                self._viewport_height = actual[1]
        except Exception as e:
            log.warning("无法检测实际视口: %s", e)

        if start_url:
            self._driver.get(start_url)

    def _cleanup_grid_sessions(self, grid_url: str) -> None:
        """Kill all existing sessions on the Grid before starting a new one."""
        import json
        import urllib.request
        try:
            status_url = grid_url.rstrip("/") + "/status"
            with urllib.request.urlopen(status_url, timeout=5) as resp:
                data = json.loads(resp.read())
            nodes = data.get("value", {}).get("nodes", [])
            for node in nodes:
                for slot in node.get("slots", []):
                    session = slot.get("session")
                    if session:
                        sid = session["sessionId"]
                        log.info("清理残留 session: %s", sid)
                        delete_url = grid_url.rstrip("/") + f"/session/{sid}"
                        req = urllib.request.Request(delete_url, method="DELETE")
                        try:
                            urllib.request.urlopen(req, timeout=5)
                        except Exception:
                            pass
        except Exception as e:
            log.debug("Grid 清理跳过: %s", e)

    def _create_local_driver(self, webdriver, browser_type: str, headless: bool):
        """Create a local WebDriver instance."""
        if browser_type == "firefox":
            options = webdriver.FirefoxOptions()
            if headless:
                options.add_argument("--headless")
            return webdriver.Firefox(options=options)
        else:
            options = webdriver.ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            return webdriver.Chrome(options=options)

    def _create_remote_driver(self, webdriver, browser_type: str,
                               headless: bool, grid_url: str):
        """Create a remote WebDriver via Selenium Grid."""
        if browser_type == "firefox":
            options = webdriver.FirefoxOptions()
            if headless:
                options.add_argument("--headless")
        else:
            options = webdriver.ChromeOptions()
            if headless:
                options.add_argument("--headless=new")

        return webdriver.Remote(
            command_executor=grid_url,
            options=options,
        )

    def teardown(self) -> None:
        if self._driver:
            self._driver.quit()

    # --- Observation ---

    def screenshot_png(self) -> bytes:
        png_bytes = self._driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(png_bytes))
        scale = img.width / self._viewport_width
        img = draw_coordinate_grid(img, scale, self._viewport_width, self._viewport_height)
        return img_to_png_bytes(img)

    def screenshot_raw(self) -> bytes:
        return self._driver.get_screenshot_as_png()

    def get_ui_tree(self) -> str:
        return "(纯视觉模式，无 DOM 树)"

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._viewport_width, self._viewport_height)

    @property
    def scale(self) -> float:
        png_bytes = self._driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(png_bytes))
        return img.width / self._viewport_width

    # --- Actions ---

    def _ensure_anchor(self):
        """Ensure a 1x1 fixed-position anchor div exists at viewport (0,0)."""
        self._driver.execute_script("""
            if (!document.getElementById('__argus_anchor')) {
                var d = document.createElement('div');
                d.id = '__argus_anchor';
                d.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;z-index:-1;pointer-events:none;';
                document.body.appendChild(d);
            }
        """)
        return self._driver.find_element("id", "__argus_anchor")

    def _move_to(self, x: int, y: int):
        """Move mouse to absolute viewport coordinates (0,0 = top-left)."""
        from selenium.webdriver.common.action_chains import ActionChains
        anchor = self._ensure_anchor()
        ActionChains(self._driver) \
            .move_to_element_with_offset(anchor, x, y) \
            .perform()

    def tap(self, x: int, y: int) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        anchor = self._ensure_anchor()
        ActionChains(self._driver) \
            .move_to_element_with_offset(anchor, x, y) \
            .click() \
            .perform()

    def input_text(self, text: str) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self._driver).send_keys(text).perform()

    def press_key(self, key: str) -> None:
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains

        # Detect remote OS: Grid nodes are typically Linux, use Ctrl
        modifier = Keys.CONTROL if self._is_remote else (Keys.COMMAND if _IS_MAC else Keys.CONTROL)
        key_map = {
            "enter": Keys.ENTER,
            "delete": Keys.BACKSPACE,
            "tab": Keys.TAB,
            "space": Keys.SPACE,
            "escape": Keys.ESCAPE,
            "select_all": modifier + "a",
        }
        mapped = key_map.get(key)
        if mapped:
            ActionChains(self._driver).send_keys(mapped).perform()
        else:
            ActionChains(self._driver).send_keys(key).perform()

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        anchor = self._ensure_anchor()
        ActionChains(self._driver) \
            .move_to_element_with_offset(anchor, x1, y1) \
            .click_and_hold() \
            .move_by_offset(x2 - x1, y2 - y1) \
            .release() \
            .perform()

    def scroll_up(self) -> None:
        self._driver.execute_script("window.scrollBy(0, -300);")

    def scroll_down(self) -> None:
        self._driver.execute_script("window.scrollBy(0, 300);")

    def open_target(self, target: str) -> None:
        self._driver.get(target)

    # --- Browser-specific actions ---

    def go_back(self) -> None:
        self._driver.back()

    def go_forward(self) -> None:
        self._driver.forward()

    def hover(self, x: int, y: int) -> None:
        from selenium.webdriver.common.action_chains import ActionChains
        anchor = self._ensure_anchor()
        ActionChains(self._driver) \
            .move_to_element_with_offset(anchor, x, y) \
            .perform()

    def _handle_platform_action(self, action: dict) -> None:
        action_type = action["type"]
        if action_type == "go_back":
            self.go_back()
        elif action_type == "go_forward":
            self.go_forward()
        elif action_type == "hover":
            w, h = self.screen_size
            x = max(0, min(int(action["x"]), w))
            y = max(0, min(int(action["y"]), h))
            self.hover(x, y)
        else:
            raise ValueError(f"Unknown action type for browser: {action_type}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return "browser"

    def get_system_prompt_segment(self) -> str:
        return BROWSER_PROMPT_SEGMENT
