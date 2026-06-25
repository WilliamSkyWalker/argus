"""Auto-dismiss known system permission dialogs and in-app overlays.

Runs at the start of each agent step before screenshot+LLM so test cases don't
need boilerplate "if X popup appears, tap Y" steps.

Per-case opt-out:
    Include `**Disable dialog auto-dismiss**: yes` in the case text. Use this
    only on cases that are explicitly testing a dialog (e.g. TC-ACC-001 for
    cold-start permissions, TC-ACC-022 for the Notification opt-in screen).

Handled (Android):
  - System runtime permission prompts (Notification / Location / Mic / Camera / ...)
    via `dumpsys window` activity match + BACK key (defaults to deny).
  - "Dark Mode Is Here" first-time overlay → tap "Got it" or BACK.
  - First-time onboarding guide overlays → BACK key.

Handled (iOS):
  - SBAlert-like system dialog with a "Don't Allow" / "Deny" / "Not Now" button
    via idb describe-all.

Untested platforms or unknown dialogs return None; the LLM agent handles them.
"""

import json
import re

from .logger import get_logger

log = get_logger("dialog_dismisser")

OPT_OUT_PATTERN = re.compile(
    r"\*\*Disable dialog auto-dismiss\*\*:\s*(yes|true|1)",
    re.IGNORECASE,
)

ANDROID_PERMISSION_ACTIVITY_MARKERS = (
    "permissioncontroller.permission.ui.GrantPermissionsActivity",
    "com.android.permissioncontroller",
)

# (marker substring in UI tree, preferred dismiss button label or None for BACK,
#  exact_match: True 时按钮 text/content-desc 必须精确等于 label，False 时 substring 包含)
# Note for Flutter-rendered dialogs (Dark Mode Is Here): the entire overlay is
# rendered as one ImageView whose content-desc concatenates all text. Looking
# up the "Got it" button by content-desc returns the full overlay's bounds, so
# the tap lands in the icon area and doesn't dismiss the dialog. BACK key
# closes the overlay reliably — use it directly here.
ANDROID_OVERLAY_DISMISS = (
    ("Dark Mode Is Here", None, False),
    # App 自定义通知权限对话框（不是系统级，dumpsys 路径找不到）
    # marker "send you notifications" 在对话框标题里独特；按钮 "Allow" 用精确匹配
    # 避免误匹配到标题文本中的 "Allow xxx to send you notifications"。
    # 主动 tap Allow 让对话框直接关掉（Don't allow 后续可能触发更多权限对话框）。
    ("send you notifications", "Allow", True),
)

# iOS labels typically present in system permission alerts / SDK auth sheets.
# Conservative list — keep to phrases unlikely to appear as regular tappable
# labels inside the app's own screens.
IOS_NEGATIVE_LABELS = (
    "Don’t Allow",  # smart quote (iOS default)
    "Don't Allow",
    "Not Now",
)


def should_dismiss(case_text: str) -> bool:
    """Return False if the case opts out of auto-dismiss."""
    if not case_text:
        return True
    return not OPT_OUT_PATTERN.search(case_text)


def dismiss_known_dialogs(platform, case_text: str = "") -> dict | None:
    """Detect and dismiss known dialogs. Returns a dict describing what was
    dismissed (caller should re-screenshot), or None if nothing to do.
    """
    if not should_dismiss(case_text):
        return None

    if hasattr(platform, "_adb"):
        return _dismiss_android(platform)
    if hasattr(platform, "_hands"):
        return _dismiss_ios(platform)
    return None


# ----------------------------- Android ----------------------------------

def _dismiss_android(platform) -> dict | None:
    # 1. System permission prompt — detected via dumpsys window (cheap).
    try:
        focus_out = platform._adb("shell", "dumpsys", "window")
        for line in focus_out.splitlines():
            if "mCurrentFocus" not in line:
                continue
            if any(m in line for m in ANDROID_PERMISSION_ACTIVITY_MARKERS):
                platform._adb("shell", "input", "keyevent", "4")
                log.info("dismissed Android system permission dialog via BACK")
                return {"kind": "android_permission", "method": "back"}
    except Exception as e:
        log.debug("Android permission focus check failed: %s", e)

    # 2. In-app overlays — need a UI tree dump.
    try:
        ui_xml = platform.get_ui_tree() or ""
    except Exception as e:
        log.debug("Android UI tree fetch failed: %s", e)
        return None

    for marker, btn_label, exact in ANDROID_OVERLAY_DISMISS:
        if marker not in ui_xml:
            continue
        if btn_label:
            bounds = _android_bounds_for_label(ui_xml, btn_label, exact=exact)
            if bounds:
                cx, cy = _android_bounds_center(bounds)
                try:
                    platform._adb("shell", "input", "tap", str(cx), str(cy))
                    log.info("dismissed overlay '%s' via tap on '%s' (%d,%d)",
                             marker, btn_label, cx, cy)
                    return {"kind": "android_overlay", "marker": marker,
                            "method": "tap", "button": btn_label}
                except Exception as e:
                    log.debug("tap dismiss failed, falling back to BACK: %s", e)
        try:
            platform._adb("shell", "input", "keyevent", "4")
            log.info("dismissed overlay '%s' via BACK", marker)
            return {"kind": "android_overlay", "marker": marker, "method": "back"}
        except Exception as e:
            log.debug("BACK dismiss failed: %s", e)

    return None


def _android_bounds_for_label(xml: str, label: str,
                                exact: bool = False) -> str | None:
    """Look up bounds="[x1,y1][x2,y2]" for a node whose content-desc or text
    matches `label`.

    exact=True 时要求 attribute 完全等于 label（避免 "Allow" 匹配到标题文本
    "Allow Foo to send notifications" 这种情况）。exact=False 是 substring。
    """
    esc = re.escape(label)
    if exact:
        # 精确匹配：(?:content-desc|text)="<label>"
        attr_pat = r'(?:content-desc|text)="' + esc + r'"'
    else:
        # Substring：attribute 含 label 即可
        attr_pat = r'(?:content-desc|text)="[^"]*' + esc + r'[^"]*"'
    # Pattern 1: attribute then bounds within same tag
    p1 = re.compile(attr_pat + r'[^/>]*?bounds="(\[\d+,\d+\]\[\d+,\d+\])"')
    m = p1.search(xml)
    if m:
        return m.group(1)
    # Pattern 2: bounds then attribute (uiautomator XMLs vary in attribute order)
    p2 = re.compile(
        r'bounds="(\[\d+,\d+\]\[\d+,\d+\])"[^/>]*?' + attr_pat
    )
    m = p2.search(xml)
    return m.group(1) if m else None


def _android_bounds_center(bounds: str) -> tuple[int, int]:
    nums = list(map(int, re.findall(r"\d+", bounds)))
    x1, y1, x2, y2 = nums[:4]
    return (x1 + x2) // 2, (y1 + y2) // 2


# ------------------------------- iOS ------------------------------------

def _dismiss_ios(platform) -> dict | None:
    hands = getattr(platform, "_hands", None)
    if not hands:
        return None
    try:
        raw = hands._idb("ui", "describe-all", "--json")
    except Exception as e:
        log.debug("iOS describe-all failed: %s", e)
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        log.debug("iOS describe-all JSON parse failed: %s", e)
        return None

    for node in _flatten_ios_nodes(data):
        label = node.get("AXLabel") or ""
        if label not in IOS_NEGATIVE_LABELS:
            continue
        frame = node.get("frame") or {}
        try:
            cx = int(frame["x"] + frame["width"] / 2)
            cy = int(frame["y"] + frame["height"] / 2)
        except (KeyError, TypeError):
            continue
        try:
            hands.tap(cx, cy)
            log.info("dismissed iOS dialog by tapping '%s' at (%d,%d)", label, cx, cy)
            return {"kind": "ios_dialog", "button": label, "method": "tap"}
        except Exception as e:
            log.debug("iOS tap failed: %s", e)
    return None


def _flatten_ios_nodes(data, out=None):
    if out is None:
        out = []
    if isinstance(data, dict):
        out.append(data)
        for v in data.values():
            _flatten_ios_nodes(v, out)
    elif isinstance(data, list):
        for v in data:
            _flatten_ios_nodes(v, out)
    return out
