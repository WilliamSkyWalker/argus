# 官网项目

品牌：**Saygo**。标语：**你说，它做。**。官网域名：[saygo.work](https://saygo.work/)。域名由 Cloudflare 管理，网站通过 GitHub Pages 部署。

独立、无构建依赖的 Saygo 响应式官网初稿，已接入用户提供的 `Video.mp4` 演示视频（约 2 分 28 秒，H.264 / AAC），并从视频生成封面。

## 本地预览

在本目录运行：

```sh
python3 -m http.server 8080 --bind 127.0.0.1
```

打开 http://127.0.0.1:8080 。

## 配置视频

把视频复制到 `public/videos/`，在 `site-config.js` 中填写 `videoSrc`，例如 `public/videos/brand.mp4`。也可填写 HTTPS 视频直链。普通视频平台的网页链接不能直接作为原生播放器的视频源，需另行接入平台嵌入播放器。

`brand`、`intro`、`videoTitle` 与 `videoPoster` 分别设置品牌名、简介、视频标题和封面。

视频源尚未配置时，页面显示预告占位。播放支持原生进度、音量和全屏控件，不自动播放。

## 发布

推送本目录变更到 `main` 会触发 `.github/workflows/website.yml`，仅将本目录发布到 GitHub Pages。

仓库 Pages 使用 GitHub Actions 发布源，自定义域名设为 `saygo.work`。Cloudflare DNS 的 `@` 添加四条 A 记录：`185.199.108.153`、`185.199.109.153`、`185.199.110.153`、`185.199.111.153`，首次配置使用 DNS only。可选 `www` CNAME 指向 `williamskywalker.github.io`。GitHub 证书就绪后开启强制 HTTPS。
