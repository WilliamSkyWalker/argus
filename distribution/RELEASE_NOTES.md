# Argus 0.4.0 — CLI / Agent integration beta

CLI 安装包已开放下载。支持 Claude Code、Codex、Qoder 和 QoderCN 的 MCP / Skill 集成，以及 Chrome / Edge 浏览器桥接。

**桌面应用仍在开发测试中，本次不发布桌面安装包。**

## 安装 / Install

需要 Python 3.10+ 和你要使用的 Agent CLI。在可写目录中复制一条命令即可自动下载并运行安装器，无需手动去 GitHub 下载：

macOS / Linux / WSL:

```sh
curl -fL https://github.com/WilliamSkyWalker/argus/releases/download/v0.4.0/install-argus-0.4.0.py -o install-argus-0.4.0.py && python3 install-argus-0.4.0.py
```

Windows PowerShell:

```powershell
Invoke-WebRequest https://github.com/WilliamSkyWalker/argus/releases/download/v0.4.0/install-argus-0.4.0.py -OutFile install-argus-0.4.0.py -ErrorAction Stop; py -3 install-argus-0.4.0.py
```

安装器会自动检测客户端、下载源码并校验 SHA256，然后安装隔离运行环境和集成。无需 Git 或 pyenv；依赖下载需要网络。WSL 用户请在 Agent CLI 所在的 WSL 环境中执行。

也可以同时下载 `argus-0.4.0.zip`，放到安装器旁边：

```sh
python3 install-argus-0.4.0.py --archive argus-0.4.0.zip
```

Windows 请将 `python3` 换为 `py -3`。可用 `--client codex`、`--client claude`、`--client qoder`、`--client qodercn` 或 `--client all` 指定客户端；手机控制额外加 `--mobile`。

## 浏览器 / Browser

按安装器输出的路径，在 Chrome / Edge 扩展页面开启开发者模式，选择“加载已解压的扩展”，然后在扩展中点击 **Connect local bridge**。重启 Agent CLI 后开始使用。

MCP 浏览器控制使用扩展后端，不使用 Playwright。无需为 Argus 配置模型 API Key，由外部 Agent 决策。扩展尚未发布到 Chrome Web Store。

## Assets

- `install-argus-0.4.0.py` — 推荐入口，带源码校验的安装器。
- `argus-0.4.0.zip` — 配合安装器使用的源码包。
- `argus-browser-0.4.0-development.zip` — 可选的浏览器开发版扩展。
- `SHA256SUMS` / `release-manifest.json` — 校验值与版本元数据。

This is a beta CLI distribution. Installer integrity, client configuration and native-host protocol checks have passed; clean-machine installation and platform-wide acceptance testing remain incomplete. Desktop installers are not included.

[完整安装、更新与卸载说明](https://github.com/WilliamSkyWalker/argus/blob/v0.4.0/distribution/README.md)
