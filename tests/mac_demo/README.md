# mac_demo

macOS 桌面自动化演示：用系统自带「计算器」做一次加法并校验结果。

- **平台**: mac
- **备注**: 跑前 `.env` 设 `MAC_APP=Calculator`（或 `MAC_APP=Calculator saygo run mac_demo`）；需 pyautogui + pyobjc 且终端已授予辅助功能/屏幕录制权限。

后台模式另有独立原生窗口测试，不使用模型，也不操作用户文档：

```sh
SAYGO_TEST_MAC_BACKGROUND=1 python3 -m unittest discover -s tests/mac_demo -p test_background_live.py -v
```

安装 `.[mac]`，开启屏幕录制和辅助功能权限，并保持桌面解锁；测试期间不要移动鼠标。
测试验证原生按钮、中文/emoji 输入与删除、垂直滚动，以及前台 PID、鼠标和剪贴板保持不变。
五项实机测试还覆盖 CLI 跨进程重连，以及 MCP stdio 连接、截图和基于截图的点击。
它不代表所有应用或其他桌面 Space 都支持后台输入。使用方式及限制见 [control](../../docs/control.md#macos-background-mode)。
