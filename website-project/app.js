"use strict";
const config = window.siteConfig || {};
const clients = document.querySelectorAll("[data-client]");
const os = document.querySelector("#install-os");
const command = document.querySelector("#install-command");
const copyButton = document.querySelector("#copy-command");
const copyStatus = document.querySelector("#copy-status");
let selectedClient = "codex";
let copyTimer;
let commandRevision = 0;
function resetCopy() {
  clearTimeout(copyTimer);
  copyButton.textContent = "复制命令 ⧉";
  copyStatus.textContent = "";
}
function updateCommand() {
  commandRevision += 1;
  const python = os.value === "windows" ? "py -3" : "python3";
  command.textContent = config.installMode === "pypi"
    ? `pipx install "saygo-agent-control[mcp]"\nsaygo setup --client ${selectedClient}`
    : `git clone https://github.com/WilliamSkyWalker/saygo.git\ncd saygo\n${python} scripts/install_agent_plugin.py --client ${selectedClient}`;
  clients.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.client === selectedClient)));
  resetCopy();
}
if (config.installMode === "pypi") {
  document.querySelector("#install-requirements").textContent = "Python 3.10+ · pipx";
  document.querySelector("#setup-requirements").textContent = "准备好 Python 3.10+、pipx 和对应的 AI 客户端，在终端运行上方命令。";
  document.querySelector("#install-mode-note").textContent = "通过 pipx 安装，再运行 saygo setup 接入 AI 工具。";
  os.closest("label").hidden = true;
}
updateCommand();
clients.forEach(button => button.addEventListener("click", () => {
  selectedClient = button.dataset.client;
  updateCommand();
}));
os.addEventListener("change", updateCommand);
copyButton.addEventListener("click", async () => {
  const revision = commandRevision;
  try {
    await navigator.clipboard.writeText(command.textContent);
    if (revision !== commandRevision) return;
    copyButton.textContent = "已复制 ✓";
    copyStatus.textContent = "安装命令已复制。";
  } catch {
    if (revision !== commandRevision) return;
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(command);
    selection.removeAllRanges();
    selection.addRange(range);
    command.parentElement.focus();
    copyButton.textContent = "请手动复制";
    copyStatus.textContent = "自动复制不可用，已选中命令，请按 Ctrl+C 或 Command+C 复制。";
  }
  clearTimeout(copyTimer);
  copyTimer = setTimeout(resetCopy, 5000);
});
document.querySelector("#year").textContent = new Date().getFullYear();
const video = document.querySelector("#video");
if (config.videoSrc) video.src = config.videoSrc;
if (config.videoPoster) video.poster = config.videoPoster;
video.addEventListener("error", () => {
  document.querySelector("#video-error").hidden = false;
});
