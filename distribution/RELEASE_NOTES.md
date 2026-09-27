# Saygo 0.4.3 — CLI / Agent integration beta

Saygo 品牌的 CLI 安装包已构建，待发布。支持 Claude Code、Codex、Qoder 和 QoderCN 的 MCP / Skill 集成，以及 Chrome / Edge 浏览器桥接。

**桌面应用仍在开发测试中，本次不发布桌面安装包。**

## 本次变更

- 修复 macOS 符号链接临时目录导致安装资源路径检查误报的问题。

- 增加 Python wheel / sdist 与 `saygo setup`，安装后无需源码目录即可配置 Agent 插件。
- PyPI 发布流程包含独立环境安装与跨平台准备检查；首次上传需配置 Trusted Publisher。

- 统一 CLI、Python 包、MCP、插件和浏览器桥接的 Saygo 名称。
- 新安装使用 `saygo`、`saygo-device` 和 `~/.saygo`，不提供旧命令别名。
- 旧品牌安装需要重新安装并连接浏览器扩展；旧配置和会话不会自动迁移或删除。

## 安装 / Install

需要 Python 3.10+ 和你要使用的 Agent CLI。在可写目录中复制一条命令即可自动下载并运行安装器，无需手动去 GitHub 下载：

macOS / Linux / WSL:

```sh
curl -fL https://raw.githubusercontent.com/WilliamSkyWalker/saygo/main/scripts/install_saygo.py -o install-saygo.py && python3 install-saygo.py
```

Windows PowerShell:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/WilliamSkyWalker/saygo/main/scripts/install_saygo.py -OutFile install-saygo.py -ErrorAction Stop; py -3 install-saygo.py
```

这条固定命令每次都会选择最新发布版（包含测试版），以后升级也用同一条命令。加 `--channel stable` 可仅选择稳定版。

安装器会自动检测客户端、下载源码并校验 SHA256，然后安装隔离运行环境和集成。无需 Git 或 pyenv；依赖下载需要网络。WSL 用户请在 Agent CLI 所在的 WSL 环境中执行。

也可以同时下载 `saygo-0.4.3.zip`，放到安装器旁边：

```sh
python3 install-saygo-0.4.3.py --archive saygo-0.4.3.zip
```

Windows 请将 `python3` 换为 `py -3`。可用 `--client codex`、`--client claude`、`--client qoder`、`--client qodercn` 或 `--client all` 指定客户端；手机控制额外加 `--mobile`。

## 更新提醒与自动更新

- 默认每 24 小时最多检查一次，Agent 启动时后台检查，不阻塞连接。
- 新版提醒显示在会话查询结果和客户端日志中。
- 安装时加 `--auto-update --update-channel beta` 开启自动更新（当前为测试版频道）。
- 下载包经过 SHA256 校验；新运行环境准备并检查成功后，在下一次空闲启动时启用。
- 离线、下载失败或准备失败时继续使用旧版；旧运行环境保留。
- 浏览器桥接、扩展或客户端 Skill 改变时提示使用完整安装器更新。
- 0.4.0 用户需要运行一次 0.4.3 安装器，以获得更新功能。

macOS / Linux / WSL 管理命令（Windows 将 `python3` 换为 `py -3`）：

```sh
python3 "$HOME/.local/share/saygo/agent-plugin/update.py" --check
python3 "$HOME/.local/share/saygo/agent-plugin/update.py" --auto on --channel beta
python3 "$HOME/.local/share/saygo/agent-plugin/update.py" --auto off
```

## 浏览器 / Browser

**[下载 Chrome / Edge 浏览器插件 ZIP](https://github.com/WilliamSkyWalker/saygo/releases/download/v0.4.3/saygo-browser-0.4.3-development.zip)**

解压 ZIP，打开 `chrome://extensions`（Edge 为 `edge://extensions`），开启开发者模式，点击“加载已解压的扩展”，选择包含 `manifest.json` 的解压目录。先运行上面的 CLI 安装命令准备本机桥接，再在扩展中点击 **Connect local bridge**。

按安装器输出的路径，在 Chrome / Edge 扩展页面开启开发者模式，选择“加载已解压的扩展”，然后在扩展中点击 **Connect local bridge**。重启 Agent CLI 后开始使用。

MCP 浏览器控制使用扩展后端，不使用 Playwright。无需为 Saygo 配置模型 API Key，由外部 Agent 决策。扩展尚未发布到 Chrome Web Store。

## Assets

- `install-saygo-0.4.3.py` — 推荐入口，带源码校验的安装器。
- `saygo-0.4.3.zip` — 配合安装器使用的源码包。
- `saygo-browser-0.4.3-development.zip` — 可选的浏览器开发版扩展。
- `SHA256SUMS` / `release-manifest.json` — 校验值与版本元数据。

This is a beta CLI distribution. Installer integrity, client configuration and native-host protocol checks have passed; clean-machine installation and platform-wide acceptance testing remain incomplete. Desktop installers are not included.

[完整安装、更新与卸载说明](https://github.com/WilliamSkyWalker/saygo/blob/v0.4.3/distribution/README.md)
