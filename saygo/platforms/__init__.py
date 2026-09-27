"""Platform registry and factory."""

from .base import Platform

AVAILABLE_PLATFORMS = ("ios", "android", "browser", "appium",
                       "mac", "macos", "windows", "win", "desktop", "rdp")


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
        # WSL 通过内置 PowerShell runner 操作当前 Windows 控制台，无需装 Windows Python。
        import platform as _p
        import shutil
        if "microsoft" in _p.release().lower() or (config.get("win") or {}).get("background"):
            if not shutil.which("powershell.exe"):
                raise RuntimeError("Windows background control requires powershell.exe; foreground fallback is disabled")
            from .windows_runner import WindowsRunnerPlatform
            return WindowsRunnerPlatform()
        # 原生 Windows Python 仍使用 pyautogui + pywin32。
        from .desktop_win import DesktopWinPlatform
        return DesktopWinPlatform()
    elif platform_name == "rdp":
        # Linux/WSL 上的 FreeRDP 客户端直连 Windows 图形会话；无需 Windows 侧 Agent。
        from .rdp import RDPPlatform
        return RDPPlatform()
    elif platform_name == "desktop":
        # 泛桌面：按运行 saygo 的 OS 自动分流到 mac / windows。
        import platform as _p
        sysname = _p.system()
        if sysname == "Windows":
            return create_platform("windows", config)
        from .desktop_mac import DesktopMacPlatform
        return DesktopMacPlatform()
    else:
        raise ValueError(
            f"Unknown platform: {platform_name}. "
            f"Available: {', '.join(AVAILABLE_PLATFORMS)}"
        )
