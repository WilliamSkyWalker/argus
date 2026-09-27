"use strict";
const config = window.siteConfig || {};
document.querySelectorAll("[data-brand]").forEach(node => { node.textContent = config.brand || "Saygo"; });
document.title = `${config.brand || "Saygo"} — 你说，它做。`;
if (config.intro) document.querySelector("[data-intro]").textContent = config.intro;
document.querySelector("[data-video-title]").textContent = config.videoTitle || "品牌介绍";
document.querySelector("#year").textContent = new Date().getFullYear();
const video = document.querySelector("#video");
const placeholder = document.querySelector("#placeholder");
if (config.videoSrc) {
  video.addEventListener("error", () => {
    video.hidden = true;
    placeholder.hidden = false;
    document.querySelector("#video-status").textContent = "视频暂时无法播放，请稍后再试";
  });
  video.src = config.videoSrc;
  if (config.videoPoster) video.poster = config.videoPoster;
  video.hidden = false;
  placeholder.hidden = true;
}
