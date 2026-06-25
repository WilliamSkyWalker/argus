"""Platform registry and factory."""

from .base import Platform

AVAILABLE_PLATFORMS = ("ios", "android", "browser")


def create_platform(platform_name: str, config: dict) -> Platform:
    """Create a platform instance by name."""
    if platform_name == "ios":
        from .ios import IOSPlatform
        return IOSPlatform()
    elif platform_name == "android":
        from .android import AndroidPlatform
        return AndroidPlatform()
    elif platform_name == "browser":
        from .browser import BrowserPlatform
        return BrowserPlatform()
    else:
        raise ValueError(
            f"Unknown platform: {platform_name}. "
            f"Available: {', '.join(AVAILABLE_PLATFORMS)}"
        )
