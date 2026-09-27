"use strict";
const config = window.siteConfig || {};
const { t } = window.siteI18n;
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
  copyButton.textContent = t("copyDefault");
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
function updateInstallText() {
  const fromPyPI = config.installMode === "pypi";
  document.querySelector("#install-requirements").textContent = fromPyPI ? "Python 3.10+ · pipx" : "Python 3.10+ · Git";
  document.querySelector("#setup-requirements").textContent = t(fromPyPI ? "setupRequirementsPypi" : "setupRequirementsSource");
  document.querySelector("#install-mode-note").textContent = t(fromPyPI ? "installNotePypi" : "installNoteSource");
  os.closest("label").hidden = fromPyPI;
}
updateInstallText();
document.querySelector("#site-language").addEventListener("change", event => {
  window.siteI18n.apply(event.target.value, true);
  updateInstallText();
  updateCommand();
});
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
    copyButton.textContent = t("copied");
    copyStatus.textContent = t("copySuccess");
  } catch {
    if (revision !== commandRevision) return;
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(command);
    selection.removeAllRanges();
    selection.addRange(range);
    command.parentElement.focus();
    copyButton.textContent = t("copyManual");
    copyStatus.textContent = t("copyFallback");
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
