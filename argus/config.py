"""Configuration management — loads from project .env file"""

import os
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
    # Skills (comma-separated, or "all" / "none")
    "SKILLS_ENABLED": "loading_detector,keyboard_detector,scroll_map,element_marker,visual_diff,toast_detector",
    "SKILLS_OCR_LANGS": "ch_sim,en",
    "SKILLS_OCR_GPU": "false",
}


def _parse_skills_config(values: dict) -> dict:
    """Parse skills configuration from flat env vars into nested dict."""
    enabled_str = values.get("SKILLS_ENABLED", "element_marker,visual_diff,smart_crop")

    if enabled_str.lower() == "none":
        enabled = []
    elif enabled_str.lower() == "all":
        enabled = [
            "loading_detector", "keyboard_detector", "scroll_map",
            "element_marker", "visual_diff", "toast_detector",
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
                values[key.strip()] = val.strip()

    # Environment variables override .env (used by --bg mode)
    for key in DEFAULT_CONFIG:
        env_val = os.environ.get(key)
        if env_val is not None:
            values[key] = env_val

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

    return {
        "platform": values["PLATFORM"],
        "llm": {
            "provider": values["LLM_PROVIDER"],
            "model": values["LLM_MODEL"],
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
