# Saygo 官网

面向 AI 工具用户和开发者的静态官网，域名 https://saygo.work/。

当前版本包括首屏安装入口、现有 QQ 音乐实机视频和快速接入三个步骤。采用白底、浅灰分区和橙色强调。

## 本地预览

```sh
python3 -m http.server 8080 --bind 127.0.0.1
```

打开 http://127.0.0.1:8080。无需构建依赖。

## 安装入口

首屏支持 Codex / Claude Code 和 macOS、Linux、WSL / Windows 切换。默认使用已发布的 PyPI 包，显示 `pipx install` 与 `saygo setup`。在 `site-config.js` 中设置 `installMode: "source"` 可切换回源码安装。复制使用 Clipboard API，失败时选中命令供手动复制。

## 视频

保留 `public/videos/brand.mp4` 和 `poster.jpg`，通过 `site-config.js` 配置视频地址与封面。播放器不自动播放，使用原生控件，播放出错时显示文件入口。

## 部署

推送本目录变更到 main 后，由 `.github/workflows/website.yml` 发布到 GitHub Pages。域名由 Cloudflare 管理。
