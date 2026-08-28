"""Configuration management — loads from project .env file"""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_CONFIG = {
    "PLATFORM": "ios",
    # LLM — OpenRouter 默认（聚合多家供应商，OpenAI 兼容协议）
    # 配 .env 时设 LLM_API_KEY=sk-or-v1-... + LLM_MODEL=<provider/model>，例如：
    #   google/gemini-2.5-flash / anthropic/claude-sonnet-4.5 / openai/gpt-4o
    # 也可改 LLM_BASE_URL 指回其他 OpenAI 兼容端点
    "LLM_PROVIDER": "openrouter",
    "LLM_MODEL": "google/gemini-2.5-flash",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "https://openrouter.ai/api/v1",
    # 分级模型路由（借鉴 midscene 按 intent 选模型）：不同用途可挂不同模型，
    # 都为空则统一回落到 LLM_MODEL（默认行为不变）。共用 LLM_BASE_URL / LLM_API_KEY，
    # 除非元素定位（locator）单独指定端点（见下）。
    #   LLM_MODEL_BRAIN     —— step 决策/视觉验证（空=用 LLM_MODEL）
    #   LLM_MODEL_PLANNER   —— 开跑前拆剧本（可用更便宜/更快的模型）
    #   LLM_MODEL_LOCATOR —— 专用元素定位小模型；**空=关闭定位兜底**（见 #2）。
    #     可选专用元素定位 VLM：bytedance/ui-tars-1.5-7b 等 UI-TARS 系，或 Claude/Gemini
    #     某档（如 anthropic/claude-sonnet-5，computer-use 血统、与 brain 不同家族更能加信号）。
    "LLM_MODEL_BRAIN": "",
    "LLM_MODEL_PLANNER": "",
    "LLM_MODEL_LOCATOR": "",
    # 定位模型的独立端点（留空则复用 LLM_BASE_URL / LLM_API_KEY）。
    # 想把定位模型指到另一家供应商（如自部署 UI-TARS）时才需要。
    "LLM_LOCATOR_BASE_URL": "",
    "LLM_LOCATOR_API_KEY": "",
    # ── 旧默认（DashScope / Qwen）保留参考；需要切回时反注释下面四行替换上面 ──
    # "LLM_PROVIDER": "qwen",
    # "LLM_MODEL": "qwen-vl-max",
    # "LLM_API_KEY": "",
    # "LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    # 可选 OpenRouter 归因头 — 默认空，非 OpenRouter 供应商无视。
    # 仅在 .env 显式配置时才会透传到 OpenAI client 的 default_headers。
    "LLM_HTTP_REFERER": "",
    "LLM_X_TITLE": "",
    # LLM 输出 token 上限（brain 决策 + planner 共用）。reasoning 模型（如
    # gemini-3.5-flash）会先烧一坨 reasoning_tokens，预算太小会截断 JSON，调大些。
    "LLM_MAX_TOKENS": "8192",
    # iOS
    "SIMULATOR_DEVICE_NAME": "Argus",
    "SIMULATOR_DEVICE_TYPE": "iPhone 16 Pro",
    "SIMULATOR_UDID": "",
    "SIMULATOR_DEVICE_MODE": "auto",  # auto | simulator | device
    # Android
    "ANDROID_SERIAL": "",
    # 被测包名：**无默认值**（空字符串）。在 .env 里配 ANDROID_PACKAGE=<你的包名>，
    # 或跑测时 `ANDROID_PACKAGE=… python3 -m argus.cli run …` 临时覆盖。
    # 跑 Android 时若为空 → 直接报错，绝不静默兜底（防止测错 App）。
    "ANDROID_PACKAGE": "",
    # macOS 桌面（前台窗口级驱动）
    # 被测 App 名（= 菜单栏显示名 / CGWindowOwnerName，如 Calculator / 备忘录）。
    # 跑 mac 平台必填；为空 → 直接报错（同 ANDROID_PACKAGE 的防误测策略）。
    "MAC_APP": "",
    # Windows 桌面（前台窗口级驱动，平移 mac 方案）
    # 被测窗口标题子串（= 任务栏/标题栏文字，如 "计算器" / "记事本" / "Calculator"）。
    # 跑 windows 平台必填；为空 → 直接报错（同上防误测）。大小写不敏感、按子串匹配。
    "WIN_APP": "",
    # 可选：被测 App 的可执行文件路径 / 启动名（如 notepad / "C:\\Path\\app.exe"）。
    # 填了则 setup 时先启动它再找窗口；空则只在已开的窗口里按 WIN_APP 找。
    "WIN_LAUNCH": "",
    # Appium（iOS + Android 统一后端）
    "APPIUM_SERVER_URL": "",          # 空 → 默认 http://127.0.0.1:4723（server 由 argus 自动起）
    "APPIUM_DEVICE": "",              # udid / adb serial；空 → android 用 ANDROID_SERIAL，ios 用 SIMULATOR_UDID
    # mjpeg 帧流截图（借鉴 midscene 的常驻视频流）：起 session 时开 Appium driver 的
    # mjpegServerPort，截图从常驻流里取最新帧，省掉每次 get_screenshot_as_png 的
    # HTTP 往返 + 设备端现场编码（agent 循环每 step 都截图，累计提速明显）。
    # 取不到帧时无条件 fallback 到 get_screenshot_as_png，故开着是安全的。
    "APPIUM_MJPEG_ENABLED": "true",
    "APPIUM_MJPEG_PORT": "",          # 空 → 默认 9100；多设备并行需各给不同端口
    "APPIUM_MJPEG_QUALITY": "90",     # JPEG 质量（1-100）；高些减少 visual_diff 噪声
    # iOS 真机 WDA 签名（`argus run --platform ios` 必填 team_id）：
    "IOS_TEAM_ID": "",               # Apple 开发者 team id（xcodeOrgId，10 位，在开发者后台查）
    "IOS_SIGNING_ID": "Apple Development",
    "IOS_WDA_BUNDLE_ID": "",         # WDA bundle id，用你 team 名下前缀，如 com.yourco.wda
    "IOS_BUNDLE_ID": "",             # 被测 App 的 bundle id（可选，不填则附着当前前台）
    # Browser
    "BROWSER_TYPE": "chrome",
    "BROWSER_HEADLESS": "false",
    "BROWSER_VIEWPORT_WIDTH": "1280",
    "BROWSER_VIEWPORT_HEIGHT": "720",
    "BROWSER_START_URL": "",
    "SELENIUM_GRID_URL": "",  # e.g. http://localhost:4444/wd/hub
    # Figma
    "FIGMA_TOKEN": "",
    "FIGMA_FILE_KEY": "",
    # Agent
    # AGENT_MAX_STEPS：整个 scenario 的 turn 绝对兜底；<=0 = 禁用（默认），
    # 由 agent.py 的 per-step MAX_TURNS_WITHOUT_PROGRESS 收敛。>0 则作硬顶。
    "AGENT_MAX_STEPS": "0",
    "AGENT_STEP_DELAY": "1.0",
    # 断言型 step（Then/But）抓多少张连续帧喂给 brain（借鉴 midscene 时间窗断言）：
    # 让「出现过又消失」的 toast/banner 等瞬态 UI 可被判断。<=1 = 关闭（只发单帧）。
    # 配合 mjpeg 时几乎零成本；无 mjpeg 时断言 step 会多截几张图。
    "AGENT_ASSERT_BURST_FRAMES": "3",
    # settle 闸（性能优化 Phase 1，见 docs/perf-plan.md）：截图前先等屏幕"加载完/稳定"
    # 再决策/采样——全程零 LLM（visual_diff 帧稳 + 状态栏噪声 mask + 超时兜底）。
    # When 用稳态 1 帧；Then 采首/中/稳三点 + 2% 路由（静态→1 图，动态→3 图）。
    # 默认开；关掉则退回"每 turn 单帧截图 + 无条件多帧断言"的旧路径。
    "AGENT_SETTLE_ENABLED": "true",
    "AGENT_SETTLE_TIMEOUT": "6.0",      # settle 轮询最长等待秒数（到时回退同步判）
    "AGENT_SETTLE_INTERVAL": "0.3",     # settle 轮询帧间隔秒
    "AGENT_SETTLE_STABLE_FRAMES": "2",  # 连续几对相邻帧"没动"判定稳定
    # wait_for（性能优化 Phase 2）：一个 step 最多花多少**墙钟秒**在"等待"上（等加载 /
    # 等结果出现）。brain 主动 wait 的轮**不计入** MAX_TURNS_WITHOUT_PROGRESS——否则
    # 慢加载（>15 轮才出结果）会被无进展上限误判假失败；超出此预算才恢复计数、最终收敛。
    "AGENT_WAIT_MAX_S": "45",
    # 连续断言合并（性能优化 Phase 3）：连续 Then/And（同屏、中间无操作步）合成 1 次大模型
    # 调用逐条判，一次推进多步——省调用次数。反偷懒硬墙逐条套用 + 去重 + 负向断言加压
    # （见 step_validator.validate_assertion_batch）。有 fail 先探测弹窗→关掉→重截重判
    # （合并同步 inline、设备还在那屏，可关弹窗），避免"弹窗假 fail"。**默认开**。
    "AGENT_MERGE_ASSERTS": "true",
    # 连续多少次 no_effect 触发元素定位兜底（见 #2）；仅当配了
    # LLM_MODEL_LOCATOR 才生效。到网格兜底(3 次)之前先试定位模型精定位。
    "AGENT_LOCATE_RETRY": "2",
    # 分层执行（借鉴 midscene planning+定位 双模型）：开启后**操作步**(When/Given)
    # 不调大 LLM，改用 planner 预规划的结构化动作 + 元素定位直接执行；**检查步**
    # (Then/But) 仍走大 LLM 深度视觉验证（反谎报硬墙不变）。操作步连续失败会逃生回大 LLM。
    # 目的：把大 LLM 从"走流程"里省出来，只用在"判断页面对不对"。默认关（不动现有路径）。
    # 依赖元素定位（LLM_MODEL_LOCATOR）来定位 tap/input 目标。
    "AGENT_SPLIT_ACT_CHECK": "false",
    # Probes —— 非视觉断言插件（埋点/后端落库/上报日志，见 argus/probes/）。
    # 用例里 `# argus-probe: <name> k=v` 声明的 step **不进 LLM**，直接跑插件拿 verdict。
    # 注册表默认 .argus/probes.json（gitignored，含连接串/密钥）；下面两项是全局覆盖，
    # 留空则用注册表里的 defaults / 单个 probe 的设置。
    # PROBE_TIMEOUT_S：一个断言最多花多少墙钟秒等数据（埋点批量上报有分钟级延迟，
    #   查太早的 0 行不算证据 —— 超预算才判 fail）。PROBE_POLL_INTERVAL_S：重查间隔。
    "PROBES_CONFIG": "",
    # PROBES_MODE：all（默认，正常跑）| skip（probe step 标 skip 直接推进，用于数据
    #   通道挂了或只关心 UI 回归）| only（只跑声明了 probe 的 case —— 由 cli 层筛，
    #   命中的 case 里 UI 步照跑，因为埋点得靠操作触发出来）。
    #   一般由 `argus run --skip-probes / --only-probes` 设置。
    "PROBES_MODE": "all",
    "PROBE_TIMEOUT_S": "",
    "PROBE_POLL_INTERVAL_S": "",
    # Skills (comma-separated, or "all" / "none")
    "SKILLS_ENABLED": "loading_detector,keyboard_detector,scroll_map,visual_diff,toast_detector",
    "SKILLS_OCR_LANGS": "ch_sim,en",
    "SKILLS_OCR_GPU": "false",
}


def _parse_skills_config(values: dict) -> dict:
    """Parse skills configuration from flat env vars into nested dict."""
    enabled_str = values.get("SKILLS_ENABLED", "visual_diff,smart_crop")

    if enabled_str.lower() == "none":
        enabled = []
    elif enabled_str.lower() == "all":
        enabled = [
            "loading_detector", "keyboard_detector", "scroll_map",
            "visual_diff", "toast_detector",
            "smart_crop", "ocr", "color_validator", "layout_checker",
        ]
    else:
        enabled = [s.strip() for s in enabled_str.split(",") if s.strip()]

    ocr_langs = [l.strip() for l in values.get("SKILLS_OCR_LANGS", "ch_sim,en").split(",")]
    ocr_gpu = values.get("SKILLS_OCR_GPU", "false").lower() == "true"

    return {
        "enabled": enabled,
        "ocr": {"langs": ocr_langs, "gpu": ocr_gpu},
    }


def _probes_mode(raw: str | None) -> str:
    """校验 PROBES_MODE；非法值回落 all（宁可正常跑，也不静默改变断言行为）。"""
    mode = (raw or "all").strip().lower()
    if mode in ("all", "skip", "only"):
        return mode
    print(f"[config] PROBES_MODE={raw!r} 无效（all|skip|only），按 all 处理")
    return "all"


def load_config() -> dict:
    """Load config: defaults → .env file → environment variables (highest priority)."""
    values = DEFAULT_CONFIG.copy()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                if val[:1] in ('"', "'") and val.find(val[0], 1) != -1:
                    # 引号包裹的值：取引号内原文（内含 # 保留），引号后的注释丢弃
                    val = val[1:val.find(val[0], 1)]
                else:
                    # 未加引号：剥掉行内注释（仅「空白 + #」之后的部分，
                    # 不误伤 URL 锚点等值内紧邻的 #）
                    val = re.sub(r"\s+#.*$", "", val).strip()
                values[key.strip()] = val

    # Environment variables override .env (used by --bg mode)
    for key in DEFAULT_CONFIG:
        env_val = os.environ.get(key)
        if env_val is not None:
            values[key] = env_val

    # LLM_API_BASE 不在 DEFAULT_CONFIG 里，上面的循环覆盖不到 — 单独读环境变量
    # （保持 env > .env 优先级）。env 只设了 LLM_BASE_URL 时，不该被 .env 的
    # LLM_API_BASE 经由下方 `or` fallback 压过 — 丢弃 .env 值。
    env_api_base = os.environ.get("LLM_API_BASE")
    if env_api_base is not None:
        values["LLM_API_BASE"] = env_api_base
    elif os.environ.get("LLM_BASE_URL") is not None:
        values.pop("LLM_API_BASE", None)

    # Support both LLM_BASE_URL and LLM_API_BASE (.env may use either).
    base_url = values.get("LLM_API_BASE") or values.get("LLM_BASE_URL", "")

    # OpenRouter-style optional headers — passed to OpenAI client as default_headers.
    # Empty strings are filtered out by brain.py so non-OpenRouter providers won't
    # see them.
    extra_headers = {}
    if values.get("LLM_HTTP_REFERER"):
        extra_headers["HTTP-Referer"] = values["LLM_HTTP_REFERER"]
    if values.get("LLM_X_TITLE"):
        extra_headers["X-Title"] = values["LLM_X_TITLE"]

    # 分级模型：brain/planner/locator 各自可覆盖，空则回落 LLM_MODEL。
    brain_model = values.get("LLM_MODEL_BRAIN") or values["LLM_MODEL"]
    planner_model = values.get("LLM_MODEL_PLANNER") or values["LLM_MODEL"]
    locator_model = values.get("LLM_MODEL_LOCATOR") or ""  # 空 = 关闭元素定位
    locator = {
        "model": locator_model,
        # 定位模型独立端点，留空复用主 LLM 的 base_url / api_key
        "base_url": values.get("LLM_LOCATOR_BASE_URL") or base_url,
        "api_key": values.get("LLM_LOCATOR_API_KEY") or values["LLM_API_KEY"],
        "extra_headers": extra_headers,
        "max_tokens": int(values.get("LLM_MAX_TOKENS") or 8192),
    }

    return {
        "platform": values["PLATFORM"],
        "llm": {
            "provider": values["LLM_PROVIDER"],
            # brain 读 llm.model —— 用 brain 覆盖（默认 = LLM_MODEL，行为不变）
            "model": brain_model,
            "planner_model": planner_model,
            "locator": locator,
            "api_key": values["LLM_API_KEY"],
            "base_url": base_url,
            "extra_headers": extra_headers,
            "max_tokens": int(values.get("LLM_MAX_TOKENS") or 8192),
        },
        "simulator": {
            "device_name": values["SIMULATOR_DEVICE_NAME"],
            "device_type": values["SIMULATOR_DEVICE_TYPE"],
            "udid": values["SIMULATOR_UDID"],
            "device_mode": values["SIMULATOR_DEVICE_MODE"],
        },
        "android": {
            "serial": values["ANDROID_SERIAL"],
            "package": values["ANDROID_PACKAGE"],
        },
        "mac": {
            "app": values["MAC_APP"],
        },
        "win": {
            "app": values["WIN_APP"],
            "launch": values["WIN_LAUNCH"],
        },
        "appium": {
            # os 由 create_platform 按平台名权威覆盖；这里给个合理默认
            "os": values["PLATFORM"] if values["PLATFORM"] in ("ios", "android") else "android",
            "server_url": values["APPIUM_SERVER_URL"],
            "device": (
                values["APPIUM_DEVICE"]
                or (values["ANDROID_SERIAL"] if values["PLATFORM"] == "android"
                    else values["SIMULATOR_UDID"])
            ),
            "team_id": values["IOS_TEAM_ID"],
            "signing_id": values["IOS_SIGNING_ID"],
            "wda_bundle_id": values["IOS_WDA_BUNDLE_ID"],
            "bundle_id": values["IOS_BUNDLE_ID"],
            "package": values["ANDROID_PACKAGE"],
            "mjpeg": {
                "enabled": values.get("APPIUM_MJPEG_ENABLED", "true").lower() == "true",
                "port": int(values["APPIUM_MJPEG_PORT"]) if values.get("APPIUM_MJPEG_PORT") else 0,
                "quality": int(values.get("APPIUM_MJPEG_QUALITY") or 90),
            },
        },
        "browser": {
            "type": values["BROWSER_TYPE"],
            "headless": values["BROWSER_HEADLESS"].lower() == "true",
            "viewport_width": int(values["BROWSER_VIEWPORT_WIDTH"]),
            "viewport_height": int(values["BROWSER_VIEWPORT_HEIGHT"]),
            "start_url": values["BROWSER_START_URL"],
            "grid_url": values["SELENIUM_GRID_URL"],
        },
        "figma": {
            "token": values["FIGMA_TOKEN"],
            "file_key": values["FIGMA_FILE_KEY"],
        },
        "agent": {
            "max_steps": int(values["AGENT_MAX_STEPS"]),
            "step_delay": float(values["AGENT_STEP_DELAY"]),
            "assert_burst_frames": int(values.get("AGENT_ASSERT_BURST_FRAMES") or 1),
            "settle_enabled": values.get("AGENT_SETTLE_ENABLED", "true").lower() == "true",
            "settle_timeout": float(values.get("AGENT_SETTLE_TIMEOUT") or 6.0),
            "settle_interval": float(values.get("AGENT_SETTLE_INTERVAL") or 0.3),
            "settle_stable_frames": int(values.get("AGENT_SETTLE_STABLE_FRAMES") or 2),
            "wait_max_s": float(values.get("AGENT_WAIT_MAX_S") or 45.0),
            "merge_asserts": values.get("AGENT_MERGE_ASSERTS", "false").lower() == "true",
            "locate_retry": int(values.get("AGENT_LOCATE_RETRY") or 2),
            "split_act_check": values.get("AGENT_SPLIT_ACT_CHECK", "false").lower() == "true",
        },
        "probes": {
            # 空 → argus/probes 用 .argus/probes.json（或 ARGUS_PROBES_CONFIG）
            "config_path": values.get("PROBES_CONFIG") or "",
            "mode": _probes_mode(values.get("PROBES_MODE")),
            # 空 → 用注册表里的值（见 argus/probes/__init__.py 的默认常量）
            "timeout_s": values.get("PROBE_TIMEOUT_S") or "",
            "poll_interval_s": values.get("PROBE_POLL_INTERVAL_S") or "",
        },
        "skills": _parse_skills_config(values),
    }


def init_config():
    """Create default .env file if it doesn't exist."""
    if ENV_FILE.exists():
        print(f"Config already exists: {ENV_FILE}")
        return
    lines = []
    for key, val in DEFAULT_CONFIG.items():
        lines.append(f"{key}={val}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"Config created: {ENV_FILE}")
    print("Please edit .env to add your API key.")
