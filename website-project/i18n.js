"use strict";
// All translations are authored here; HTML translations never include user input.
(() => {
  const messages = {
  "zh": {
    "platformLabel": "平台支持与计划",
    "platformTitle": "从桌面到手机，持续扩展。",
    "platformIntro": "面向 Windows、Windows WSL 和 macOS，覆盖 Android、iOS 真机与模拟器的设备操控与调试。",
    "availableLabel": "当前已可用",
    "availableTitle": "Windows · Windows WSL · Android",
    "availableDetail": "已可在 Windows 原生环境、Windows WSL 和 Android 上使用 Saygo。",
    "plannedLabel": "后续计划",
    "plannedTitle": "macOS · iOS 真机与模拟器",
    "plannedDetail": "待相关 Apple 设备到位后，将完善 macOS 支持，完成 iOS 真机与模拟器的适配和实测，逐步覆盖上述全部平台和设备类型。",
    "skip": "跳到主要内容",
    "navDemo": "实机演示",
    "navStart": "接入指南",
    "navDocs": "文档 ↗",
    "eyebrow": "<span class=\"status-dot\"></span> 为你的 AI 工具扩展行动能力",
    "heroTitle": "让你的 AI，<br>操作<span>真实设备。</span>",
    "heroDescription": "为 Codex、Claude Code 等 AI 工具连接浏览器、桌面和手机。从理解任务，到执行操作，再到确认结果。",
    "chooseOS": "选择操作系统",
    "copy": "复制命令 <span aria-hidden=\"true\">⧉</span>",
    "startLink": "查看接入步骤 <span aria-hidden=\"true\">→</span>",
    "noKey": "沿用现有 AI 工具，无需另配模型 API Key",
    "demoHeading": "<i class=\"status-dot\"></i> 实机演示",
    "demoPlatform": "桌面 / QQ 音乐",
    "taskLabel": "任务",
    "demoTask": "打开 QQ 音乐，并搜索作品。",
    "videoError": "视频暂时无法播放。<a href=\"public/videos/brand.mp4\">打开视频文件 ↗</a>",
    "watch": "<span class=\"play-mark\" aria-hidden=\"true\">▶</span> 观看完整操作过程",
    "recording": "02:28 · 真实录屏",
    "observe": "<b>01</b> 观察界面",
    "act": "<b>02</b> 执行操作",
    "verify": "<b>03</b> 确认结果",
    "compatibility": "你熟悉的 AI 工具，多一种能力。",
    "integrations": "查看接入说明 ↗",
    "quickstartLabel": "快速接入",
    "quickstartTitle": "从第一条命令开始。",
    "quickstartIntro": "继续使用熟悉的工具。<br>把屏幕上的操作，交给 AI。",
    "stepInstall": "安装插件",
    "setupRequirementsPypi": "准备好 Python 3.10+、pipx 和对应的 AI 客户端，在终端运行上方命令。",
    "installerDoes": "安装器会完成",
    "installerDetail": "创建独立运行环境，接入 MCP 与操作 Skill。",
    "chooseCopy": "选择工具并复制命令 <span aria-hidden=\"true\">↑</span>",
    "stepConnect": "连接你的设备",
    "connectDetail": "先从一个浏览器页面开始。按照安装器提示，加载 Chrome 或 Edge 扩展。",
    "extensionLabel": "浏览器扩展",
    "extensionDetail": "打开扩展弹窗，点击 <code>Connect local bridge</code>，然后重启 AI 客户端。",
    "deviceDocs": "桌面和手机接入说明 <span aria-hidden=\"true\">↗</span>",
    "stepPrompt": "发出第一条指令",
    "promptDetail": "在新的 AI 会话中描述任务，让它先查看当前界面，再开始操作。",
    "tryPrompt": "试着对 AI 说",
    "prompt": "“使用 Saygo 查看已连接的设备，连接我的测试浏览器页面，告诉我屏幕上有什么。”",
    "observeFirst": "从观察开始，确认连接成功。",
    "betaNote": "开发测试版",
    "installNotePypi": "通过 pipx 安装，再运行 saygo setup 接入 AI 工具。",
    "platformDocs": "查看平台支持与验证范围 ↗",
    "tagline": "你说，它做。",
    "footerDocs": "文档",
    "platformNote": "手机需要额外工具链，桌面控制需要相应系统权限。",
    "homeLabel": "Saygo 首页",
    "navLabel": "主导航",
    "installLabel": "插件安装命令",
    "clientLabel": "选择 AI 工具",
    "commandLabel": "安装命令",
    "videoLabel": "Saygo 操作 QQ 音乐完整实机演示",
    "workflowLabel": "工作流程",
    "languageLabel": "选择语言",
    "title": "Saygo — 让你的 AI，操作真实设备。",
    "description": "为 Codex、Claude Code 等 AI 工具连接浏览器、桌面和手机。通过命令行安装 Saygo 插件，让 AI 观察界面、执行操作并确认结果。",
    "ogDescription": "为 Codex、Claude Code 等 AI 工具连接浏览器、桌面和手机。",
    "copyDefault": "复制命令 ⧉",
    "copied": "已复制 ✓",
    "copySuccess": "安装命令已复制。",
    "copyManual": "请手动复制",
    "copyFallback": "自动复制不可用，已选中命令，请按 Ctrl+C 或 Command+C 复制。",
    "setupRequirementsSource": "准备好 Python 3.10+、Git 和对应的 AI 客户端，在终端运行上方命令。",
    "installNoteSource": "克隆源码后运行安装器，接入 AI 工具。"
  },
  "en": {
    "platformLabel": "PLATFORM SUPPORT & ROADMAP",
    "platformTitle": "From desktops to phones.",
    "platformIntro": "Our goal is device control and debugging across Windows, Windows WSL, and macOS, with physical Android and iOS devices, Android emulators, and iOS simulators.",
    "availableLabel": "AVAILABLE NOW",
    "availableTitle": "Windows · Windows WSL · Android",
    "availableDetail": "Saygo is available on native Windows, Windows WSL, and Android.",
    "plannedLabel": "PLANNED",
    "plannedTitle": "macOS · iOS devices & simulators",
    "plannedDetail": "Once the required Apple hardware is available, we will complete macOS support and integrate and test physical iOS devices and simulators, working toward coverage of the full platform lineup above.",
    "skip": "Skip to main content",
    "navDemo": "Live demo",
    "navStart": "Get started",
    "navDocs": "Docs ↗",
    "eyebrow": "<span class=\"status-dot\"></span> Give your AI tools the ability to act",
    "heroTitle": "Let your AI<br>operate <span>real devices.</span>",
    "heroDescription": "Connect Codex, Claude Code, and other AI tools to browsers, desktops, and phones. From understanding a task to taking action and verifying the result.",
    "chooseOS": "Choose your operating system",
    "copy": "Copy commands <span aria-hidden=\"true\">⧉</span>",
    "startLink": "See setup steps <span aria-hidden=\"true\">→</span>",
    "noKey": "Use your existing AI tools. No extra model API key needed.",
    "demoHeading": "<i class=\"status-dot\"></i> Live demo",
    "demoPlatform": "Desktop / QQ Music",
    "taskLabel": "Task",
    "demoTask": "Open QQ Music and search for music.",
    "videoError": "The video could not be played. <a href=\"public/videos/brand.mp4\">Open the video file ↗</a>",
    "watch": "<span class=\"play-mark\" aria-hidden=\"true\">▶</span> Watch the full task",
    "recording": "02:28 · Actual screen recording",
    "observe": "<b>01</b> Observe",
    "act": "<b>02</b> Act",
    "verify": "<b>03</b> Verify",
    "compatibility": "Your familiar AI tools, with new capabilities.",
    "integrations": "Integration guide ↗",
    "quickstartLabel": "GET STARTED",
    "quickstartTitle": "Start with one command.",
    "quickstartIntro": "Keep using the tools you know.<br>Let AI handle the actions on screen.",
    "stepInstall": "Install the plugin",
    "setupRequirementsPypi": "With Python 3.10+, pipx, and your AI client ready, run the commands above in a terminal.",
    "installerDoes": "THE INSTALLER WILL",
    "installerDetail": "Create an isolated runtime and connect the MCP server and device skill.",
    "chooseCopy": "Choose a tool and copy commands <span aria-hidden=\"true\">↑</span>",
    "stepConnect": "Connect your device",
    "connectDetail": "Start with a browser page. Follow the installer instructions to load the Chrome or Edge extension.",
    "extensionLabel": "BROWSER EXTENSION",
    "extensionDetail": "Open the extension popup, click <code>Connect local bridge</code>, then restart your AI client.",
    "deviceDocs": "Desktop and mobile setup <span aria-hidden=\"true\">↗</span>",
    "stepPrompt": "Give your first instruction",
    "promptDetail": "Describe a task in a new AI session. Ask it to observe the current screen before taking action.",
    "tryPrompt": "TRY ASKING YOUR AI",
    "prompt": "“Use Saygo to list connected devices, connect to my test browser page, and tell me what is on screen.”",
    "observeFirst": "Start by observing to confirm the connection.",
    "betaNote": "DEVELOPMENT BETA",
    "installNotePypi": "Install with pipx, then run saygo setup to connect your AI tools. ",
    "platformDocs": "Supported platforms and validation limits ↗",
    "tagline": "You say it. It does it.",
    "footerDocs": "Docs",
    "platformNote": "Mobile devices need additional tools; desktop control needs the appropriate system permissions. ",
    "homeLabel": "Saygo home",
    "navLabel": "Main navigation",
    "installLabel": "Plugin installation commands",
    "clientLabel": "Choose your AI tool",
    "commandLabel": "Installation commands",
    "videoLabel": "Full demo of Saygo operating QQ Music",
    "workflowLabel": "How it works",
    "languageLabel": "Choose language",
    "title": "Saygo — Let your AI operate real devices.",
    "description": "Connect Codex, Claude Code, and other AI tools to browsers, desktops, and phones. Install Saygo from your terminal so AI can observe, act, and verify results.",
    "ogDescription": "Connect Codex, Claude Code, and other AI tools to browsers, desktops, and phones.",
    "copyDefault": "Copy commands ⧉",
    "copied": "Copied ✓",
    "copySuccess": "Installation commands copied.",
    "copyManual": "Copy manually",
    "copyFallback": "Automatic copying is unavailable. The commands are selected; press Ctrl+C or Command+C to copy.",
    "setupRequirementsSource": "With Python 3.10+, Git, and your AI client ready, run the commands above in a terminal.",
    "installNoteSource": "Clone the source and run the installer to connect your AI tools. "
  }
};
  const storageKey = "saygo.language";
  let saved;
  try { saved = localStorage.getItem(storageKey); } catch { /* Storage may be blocked. */ }
  const browserLanguage = navigator.languages?.[0] || navigator.language || "en";
  let language = ["zh", "en"].includes(saved)
    ? saved
    : (browserLanguage.toLowerCase().startsWith("zh") ? "zh" : "en");
  const t = key => messages[language][key];
  function apply(next, persist = false) {
    if (!["zh", "en"].includes(next)) return;
    language = next;
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.title = t("title");
    document.querySelector('meta[name="description"]').content = t("description");
    document.querySelector('meta[property="og:title"]').content = t("title");
    document.querySelector('meta[property="og:description"]').content = t("ogDescription");
    document.querySelectorAll("[data-i18n]").forEach(element => {
      element.textContent = t(element.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-html]").forEach(element => {
      element.innerHTML = t(element.dataset.i18nHtml);
    });
    document.querySelectorAll("[data-i18n-aria]").forEach(element => {
      element.setAttribute("aria-label", t(element.dataset.i18nAria));
    });
    document.querySelector("#site-language").value = language;
    if (persist) {
      try { localStorage.setItem(storageKey, language); } catch { /* Switching still works. */ }
    }
  }
  window.siteI18n = { t, apply, get language() { return language; } };
  apply(language);
})();
