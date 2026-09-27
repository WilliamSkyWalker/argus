# Windows 后台视觉操作调研

调研日期：2026-09-26。以下区分公开实现与 Saygo 的建议路线，尚未实现 WGC 或独立桌面会话。

## 可以参考的实现

| 项目 | 公开方案 | 对 Saygo 的启示 |
| --- | --- | --- |
| UiPath | 区分 Simulate、Window Messages、Hardware Events；支持程度取决于应用技术 | 输入能力必须分别验证，不能把后台模式当作通用保证 |
| Power Automate PiP | Child session 提供独立 Windows 会话；Virtual desktop 模式不支持截图和物理输入 | 纯视觉操作优先研究独立会话，而非只隔离控件操作的桌面模式 |
| Microsoft UFO | 结合视觉和 UIA，窗口选择流程会设置焦点；仓库仍把 PiP Desktop 标为 coming soon | 可参考架构，不能当作已可复用的无焦点后台实现 |
| Anthropic computer-use demo | 容器内针对指定 DISPLAY 使用 xdotool 输入 | 独立显示环境可以隔离鼠标；该 Linux 示例不能直接解决 Windows 现有应用窗口 |
| OBS | WGC 以 HWND 创建捕获目标，维护帧池并处理尺寸和设备变化 | 可参考窗口捕获生命周期，截图与输入需要独立解决 |

来源：[UiPath 输入方式](https://docs.uipath.com/activities/other/latest/ui-automation/input-methods)、
[Power Automate PiP 限制](https://learn.microsoft.com/en-us/power-automate/desktop-flows/run-desktop-flows-pip)、
[UFO 窗口操作](https://github.com/microsoft/UFO/blob/main/documents/docs/mcp/servers/host_ui_executor.md)、
[UFO 实现状态](https://github.com/microsoft/UFO/blob/main/ufo/README.md)、
[Anthropic 输入实现](https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/tools/computer.py)、
[OBS 捕获实现](https://github.com/obsproject/obs-studio/blob/master/libobs-winrt/winrt-capture.cpp)。

## 建议路线

1. **先验证 HWND 定向 WGC 捕获。** Windows 常驻 worker 持有帧池，经现有桥返回图像、时间戳、窗口尺寸与坐标原点。首次取帧设超时；窗口关闭、最小化或帧停滞返回明确状态，不自动切前台。WGC 是对合成窗口捕获的候选改进，不能保证所有自绘应用都能捕获；官方示例明确不捕获最小化窗口。参见 [CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow) 和 [HWND 示例限制](https://github.com/microsoft/Windows.UI.Composition-Win32-Samples/blob/master/cpp/ScreenCaptureforHWND/README.md)。
2. **输入支持单独声明和测试。** 保留已验证的原生 Edit/Button 后台路径。自绘控件可研究窗口绑定的 UIA 执行适配，但依赖应用提供相应能力；不向 LLM 提供 UI 树。截图成功不代表 PostMessage 输入成功。SendInput 注入系统输入流，没有指定 HWND 的参数，不能提供私人鼠标。参见 [UIA providers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-providersoverview) 和 [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)。
3. **无法后台输入的应用，研究独立会话或虚拟机。** 在隔离环境内执行真实鼠标操作，用户桌面保持独立。现有应用进程、登录态不能假定可以直接迁移，需要另行验证应用多实例限制与登录流程。普通 Win+Tab 桌面不能视作这类隔离方案。

Saygo 为 MIT 项目，OBS 仅作设计参考，不复制其 GPL 实现。实现候选包括 MIT 的 [Microsoft 示例](https://github.com/microsoft/Windows.UI.Composition-Win32-Samples/blob/master/LICENSE) 和 [windows-capture](https://github.com/NiiightmareXD/windows-capture)，采用前核查具体版本与许可证声明。

## 验证范围

先用测试应用验证无遮挡、部分遮挡、完全遮挡、最小化、恢复、缩放和 DPI 变化；输入覆盖中文、emoji、多行、清空与按钮操作。记录窗口身份、帧新鲜度、前台 HWND、鼠标和剪贴板变化，避免靠 LLM 反复读图猜测是否成功。随后对自绘应用验证草稿输入，外发操作单独按用户指令执行。

现有 PrintWindow 与原生控件测试通过不等于上述方案已通过实机验证；独立会话、WGC 与自绘输入仍需各自的 PoC。
