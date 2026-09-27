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

推送到 `main` 后，由 `.github/workflows/website.yml` 校验静态资源并通过 Wrangler 自动发布到 Cloudflare Pages，项目名为 `saygo`，生产分支为 `main`。也可以在 GitHub Actions 手动运行部署。网站不需要 npm 构建。

GitHub 仓库 Actions Secrets 需要配置：

- `CLOUDFLARE_ACCOUNT_ID`：目标 Cloudflare 账户 ID。
- `CLOUDFLARE_API_TOKEN`：该账户的 Cloudflare Pages 编辑权限。

首次部署会创建 Direct Upload Pages 项目；已有项目会复用。上线后在 Pages 项目的 Custom domains 绑定 `saygo.work` 和 `www.saygo.work`，由 Cloudflare 管理 DNS 和 HTTPS。

`www` 到主域名的跳转使用域名下的 Cloudflare Redirect Rule：匹配 `http*://www.saygo.work/*`，目标为 `https://saygo.work/${2}`，状态码 301，并保留查询参数。Pages 的 `_redirects` 不支持按来源域名匹配。

本地需要手动发布时，可以运行：

```sh
npx wrangler login
npx wrangler pages deploy website-project --project-name=saygo --branch=main
```

上面部署命令在仓库根目录运行。GitHub Actions 使用 API Token，无需交互登录。
