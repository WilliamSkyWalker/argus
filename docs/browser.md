# Playwright 浏览器后端

新 `argus workflow` 的 browser 资源默认使用 Playwright，通过 CDP 连接独立运行的 Chromium。
原 QA 入口 `argus run` 继续使用 Selenium（含 Selenium Grid）。旧 device 会话没有 backend 字段时
仍按 Selenium 连接；新 `argus device start` 也保留 Selenium 默认值，可显式选择 Playwright。

Playwright 后端仍按截图和坐标操作，不向模型提供 DOM 树，也不依赖 selector 定位。
迁移新增的是持久会话接入、稳定页面身份、页面事件和显式标签页管理。

## 安装与启动

```bash
pip3 install -r requirements.txt
# 没有本地 Chrome/Chromium 时，下载 Playwright 配套 Chromium：
python3 -m playwright install chromium
python3 -m argus.cli device start --platform browser --backend playwright --serial admin-web
python3 -m argus.cli device navigate https://example.com --serial admin-web
python3 -m argus.cli device screenshot --serial admin-web --out /tmp/admin.png
```

浏览器查找顺序：`ARGUS_CHROME_BIN`、常见系统安装路径/PATH、Playwright 配套 Chromium。
支持现有 `ARGUS_BROWSER_HEADLESS`、`ARGUS_BROWSER_W`、`ARGUS_BROWSER_H`。
人工接管需要有头浏览器；无头浏览器仅适合自动化执行和集成测试。

Chrome 由独立进程启动，使用该 session 的独立用户目录。控制器退出仅断开 Playwright，
不会调用 `browser.close()` 或 `context.close()`；`device stop` 才结束命名浏览器进程。

也可以将已有的 Selenium Chrome 会话切换为 Playwright，无需重建浏览器或清理登录状态：

```bash
python3 -m argus.cli device start --platform browser --backend playwright --serial admin-web
```

若已有多个标签页且从未选择过目标，命令会拒绝任意挑选第一页。先列出页面并显式选择。

## 页面选择与弹窗

```bash
python3 -m argus.cli device pages --serial admin-web
python3 -m argus.cli device select-page PAGE_ID --serial admin-web
python3 -m argus.cli device close-page PAGE_ID --serial admin-web
```

`pages` 返回 `page_id`、URL、标题、`opener_id`、是否选中。`page_id` 是 Chromium target ID，
在浏览器存活期间跨 Python 进程稳定，不依赖页面排序或可能重复的 URL。

- 首次连接只有一个页面时可自动绑定；多个页面时必须显式选择。
- 新弹窗不会自动改变操作目标。连接期间监听的新页面事件会出现在 Runtime 动作结果中。
- `select-page` 更新会话默认页，并将 device 会话设为 Playwright 后端。
- 已选页面关闭后，后续截图/输入报错，不自动切到另一页；`pages` 和 `select-page` 仍可用于重新选页。
- 截图前会把绑定的页面切到前台，保证人工看到的页面和截图目标一致，并避免后台页面暂停渲染导致截图超时。
- 浏览器进程重启后 target ID 会变化；旧任务绑定不会静默复用到新页面，需重新规划。

JavaScript alert/confirm/prompt 不由控制器自动确认。打开这些对话框的动作可能等待或超时；
首版尚无专门的对话框接管协议，需要用户处理，并按 Runtime 的不确定动作流程核对结果。

## Workflow 接入

```json
{
  "version": 1,
  "resources": {
    "admin": {"kind": "browser", "session": "admin-web", "backend": "playwright"}
  },
  "steps": [
    {"id": "tabs", "kind": "pages", "resource": "admin"},
    {"id": "screen", "kind": "observe", "resource": "admin"}
  ]
}
```

`backend` 可以省略，Runtime 默认 Playwright；显式 `"backend": "selenium"` 保留旧适配器。
资源也可指定 `page_id` 作为初始目标。Selenium 资源不支持新页面管理接口。

切换或关闭页面通过普通 action 执行，同样记录 dispatch intent、要求最新 observation：

```json
{
  "id": "choose_login",
  "kind": "action",
  "resource": "admin",
  "observation": {"$ref": "steps.screen"},
  "action": {"type": "select_page", "page_id": "TARGET_ID_FROM_PAGES"}
}
```

`close_page` 使用相同的 `page_id` 参数。普通导航也支持 `go_back`/`go_forward`。
切换后要重新 `observe` 才能继续点击。动作输出包含当前 `page_id`、页面列表和本次连接已接收的事件。
事件不跨控制器离线期间持续采集；重连时通过现存页面列表重新发现页面。

Runtime 在观察或完成切页时将 page ID 保存到任务检查点。恢复任务使用任务自身的绑定，
不跟随另一个 CLI 控制器修改的会话默认页。截图校验同时比较 page ID 和 URL，即使两页图像完全相同或原页发生导航，也不能沿用过时的观察。
现有资源锁仍以浏览器 session 为单位，首版不支持同一个 session 的不同标签页分给多个并行任务。

## 验证与边界

```bash
python3 -m unittest discover -s tests/browser_demo -v
# 已安装 Playwright 后，指定 Chromium 执行真实浏览器集成测试：
ARGUS_TEST_CHROME=/path/to/chrome python3 -m unittest discover -s tests/browser_demo -v
python3 -m unittest discover -s tests/runtime_demo -v
```

真实集成测试只访问本机测试页面，使用临时浏览器用户目录，覆盖坐标点击、中文输入、弹窗发现、
跨进程重连、登录状态存储保留、人工接管恢复、CLI 页面管理、关闭页面后的保护以及控制器断开后浏览器存活。
它模拟登录流程，不会连接真实第三方登录或支付服务。测试浏览器为 headless；测试启动参数中的
`--no-sandbox` 仅用于兼容受限 CI 环境，不是 Argus 常规浏览器启动参数。

当前仅支持 Chromium 的 CDP 接入，尚未加入 Playwright 原生远程协议、Firefox/WebKit 或通用下载管理。
Playwright 官方说明 CDP 接入与原生连接存在功能差异，见 [connect_over_cdp](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)。
坐标点击不具备 Locator 的元素可操作性自动等待，页面稳定检测和视觉定位仍由 Argus 负责。

## Connect an existing daily browser

Use the optional [Chrome/Edge extension backend](browser-extension.md) to share existing logged-in tabs, including a Windows browser controlled from WSL. Playwright remains the managed-browser backend.

Both backends support `device new-page URL`, `select-page PAGE_ID` and `close-page PAGE_ID`; see [tab management](browser-extension.md#create-select-and-close-tabs).
