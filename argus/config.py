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
    # 除非 grounding 单独指定端点（见下）。
    #   LLM_MODEL_BRAIN     —— step 决策/视觉验证（空=用 LLM_MODEL）
    #   LLM_MODEL_PLANNER   —— 开跑前拆剧本（可用更便宜/更快的模型）
    #   LLM_MODEL_GROUNDING —— 专用视觉定位模型；**空=关闭 grounding 兜底**（见 #2）。
    #     可选专用 grounding VLM：bytedance/ui-tars-1.5-7b 等 UI-TARS 系，或 Claude/Gemini
    #     某档（如 anthropic/claude-sonnet-5，computer-use 血统、与 brain 不同家族更能加信号）。
    "LLM_MODEL_BRAIN": "",
    "LLM_MODEL_PLANNER": "",
    "LLM_MODEL_GROUNDING": "",
    # grounding 模型的独立端点（留空则复用 LLM_BASE_URL / LLM_API_KEY）。
    # 想把 grounding 指到另一家供应商（如自部署 UI-TARS）时才需要。
    "LLM_GROUNDING_BASE_URL": "",
    "LLM_GROUNDING_API_KEY": "",
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
    # 连续多少次 no_effect 触发 grounding 定位兜底（见 #2）；仅当配了
    # LLM_MODEL_GROUNDING 才生效。到网格兜底(3 次)之前先试 grounding 精定位。
    "AGENT_GROUNDING_RETRY": "2",
    # 分层执行（借鉴 midscene planning+grounding 双模型）：开启后**操作步**(When/Given)
    # 不调大 LLM，改用 planner 预规划的结构化动作 + grounding 定位直接执行；**检查步**
    # (Then/But) 仍走大 LLM 深度视觉验证（反谎报硬墙不变）。操作步连续失败会逃生回大 LLM。
    # 目的：把大 LLM 从"走流程"里省出来，只用在"判断页面对不对"。默认关（不动现有路径）。
    # 依赖 grounding（LLM_MODEL_GROUNDING）来定位 tap/input 目标。
    "AGENT_SPLIT_ACT_CHECK": "false",
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

    # 分级模型：brain/planner/grounding 各自可覆盖，空则回落 LLM_MODEL。
    brain_model = values.get("LLM_MODEL_BRAIN") or values["LLM_MODEL"]
    planner_model = values.get("LLM_MODEL_PLANNER") or values["LLM_MODEL"]
    grounding_model = values.get("LLM_MODEL_GROUNDING") or ""  # 空 = 关闭 grounding
    grounding = {
        "model": grounding_model,
        # grounding 独立端点，留空复用主 LLM 的 base_url / api_key
        "base_url": values.get("LLM_GROUNDING_BASE_URL") or base_url,
        "api_key": values.get("LLM_GROUNDING_API_KEY") or values["LLM_API_KEY"],
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
            "grounding": grounding,
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
            "grounding_retry": int(values.get("AGENT_GROUNDING_RETRY") or 2),
            "split_act_check": values.get("AGENT_SPLIT_ACT_CHECK", "false").lower() == "true",
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
