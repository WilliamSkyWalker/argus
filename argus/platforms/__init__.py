"""Platform registry and factory."""

from .base import Platform

AVAILABLE_PLATFORMS = ("ios", "android", "browser", "appium")


def create_platform(platform_name: str, config: dict) -> Platform:
    """Create a platform instance by name."""
    if platform_name in ("appium", "ios", "android"):
        # 移动端统一走 Appium。ios/android 只是把 os 预设进 config，driver 同一个。
        # （旧 adb/idb driver 已废；android.py 仅留给 MCP 的 device_* 原语，不再走这里）
        from .appium import AppiumPlatform
        if platform_name in ("ios", "android"):
            config.setdefault("appium", {})["os"] = platform_name  # 平台名权威
        return AppiumPlatform()
    elif platform_name == "browser":
        from .browser import BrowserPlatform
        return BrowserPlatform()
    else:
        raise ValueError(
            f"Unknown platform: {platform_name}. "
            f"Available: {', '.join(AVAILABLE_PLATFORMS)}"
        )
