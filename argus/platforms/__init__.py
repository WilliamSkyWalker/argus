"""Platform registry and factory."""

from .base import Platform

AVAILABLE_PLATFORMS = ("ios", "android", "browser", "appium",
                       "mac", "macos", "windows", "win", "desktop")


def create_platform(platform_name: str, config: dict) -> Platform:
    """Create a platform instance by name."""
    if platform_name in ("appium", "ios", "android"):
        # 移动端统一走 Appium。ios/android 只是把 os 预设进 config，driver 同一个。
        from .appium import AppiumPlatform
        if platform_name in ("ios", "android"):
            config.setdefault("appium", {})["os"] = platform_name  # 平台名权威
        return AppiumPlatform()
    elif platform_name == "browser":
        from .browser import BrowserPlatform
        return BrowserPlatform()
    elif platform_name in ("mac", "macos"):
        # macOS 桌面原生驱动（pyautogui，纯视觉，窗口级前台方案）。
        from .desktop_mac import DesktopMacPlatform
        return DesktopMacPlatform()
    elif platform_name in ("windows", "win"):
        # Windows 桌面原生驱动（pyautogui + pywin32，纯视觉，窗口级前台方案）。
        from .desktop_win import DesktopWinPlatform
        return DesktopWinPlatform()
    elif platform_name == "desktop":
        # 泛桌面：按运行 argus 的 OS 自动分流到 mac / windows。
        import platform as _p
        sysname = _p.system()
        if sysname == "Windows":
            from .desktop_win import DesktopWinPlatform
            return DesktopWinPlatform()
        from .desktop_mac import DesktopMacPlatform
        return DesktopMacPlatform()
    else:
        raise ValueError(
            f"Unknown platform: {platform_name}. "
            f"Available: {', '.join(AVAILABLE_PLATFORMS)}"
        )
