"""Step-progress validators — enforce 「看全局，禁跳跃」 contract.

agent.py calls these on every LLM decision. Violations are NOT silently
fixed — they are **rejected**, and the reject reason is fed back to the
LLM via `retry_feedback` so it self-corrects on the next call. This is
the hard wall that prevents LLM from gaming step_progress (claiming
multiple steps done in one shot, skipping ahead to terminal state, etc).

Two layers of validation:

1. ``validate_evidence(text)`` — the evidence text must reference a
   concrete visible screen element, not just paraphrase the step.

2. ``validate_step_progress(decision, prev_index, total_steps)`` — checks
   current_step_index monotonicity, current_step_status legality,
   evidence presence when status=pass/fail, fail_reason presence
   when status=fail.
"""

from __future__ import annotations

# ── Words that count as "concrete screen element reference" ────────
#
# Heuristic: a valid evidence string must contain at least one of these
# concrete-element tokens OR a quoted string (treated as a referenced UI
# text). Tuned for Chinese-language test cases since nb_cases is the main
# target; a few English variants kept for browser tests.

_SCREEN_ELEMENT_WORDS = (
    # UI 控件
    "按钮", "图标", "输入框", "文本框", "卡片", "列表", "菜单", "下拉",
    "tab", "Tab", "TAB", "标签页", "底栏", "顶栏", "导航栏", "侧边栏",
    "状态栏", "工具栏", "弹窗", "对话框", "提示框", "横幅", "toast",
    "Toast", "snackbar", "Snackbar", "链接", "图片", "视频", "图表",
    "进度条", "加载圈", "spinner", "Spinner", "checkbox", "Checkbox",
    "switch", "Switch", "开关", "单选", "复选",
    # 视觉/位置
    "页面", "屏幕", "顶部", "底部", "中央", "中间", "上方", "下方",
    "左侧", "右侧", "左上", "右上", "左下", "右下", "左边", "右边",
    # 状态/外观
    "高亮", "选中", "聚焦", "灰色", "红色", "绿色", "蓝色", "黄色",
    "白色", "黑色", "深色", "浅色", "暗色", "亮色", "变深", "变浅",
    "展开", "收起", "出现", "消失", "弹出", "关闭", "打开", "跳转",
    "切换", "刷新", "加载", "禁用", "可用",
    # 内容指标
    "标题", "副标题", "正文", "占位", "提示文字", "错误信息",
    # 通用兜底
    "文字", "文案", "字样", "字段", "数字", "时间", "日期",
)


def has_concrete_screen_reference(text: str) -> bool:
    """True if ``text`` mentions a concrete screen element or quoted UI text."""
    if not text:
        return False
    # quoted strings — common pattern: 「设置」 / "Submit" / “登录” → treated as
    # references to actual on-screen text
    for opener, closer in ("「」", '""', "''", "“”", "‘’", "《》"):
        if opener in text and closer in text:
            return True
    if '"' in text or "'" in text:
        return True
    return any(w in text for w in _SCREEN_ELEMENT_WORDS)


def validate_evidence(text: str) -> tuple[bool, str]:
    """Check an evidence string.

    Returns ``(ok, reject_reason)``. ``reject_reason`` is a short Chinese
    instruction fed back to the LLM verbatim — it should describe what's
    missing, not how to fix it (give the model room to comply).
    """
    if not text or not text.strip():
        return False, "evidence 字段为空。current_step_status=pass/fail 时必须填写当前截图里能验证该 step 的具体证据。"
    s = text.strip()
    if len(s) < 15:
        return False, (
            f"evidence 太短（{len(s)} 字符），至少 15 字符。"
            "需要描述屏幕上具体看到了什么元素 / 文字 / 位置 / 颜色，而不是简单说「已完成」。"
        )
    if not has_concrete_screen_reference(s):
        return False, (
            "evidence 没有引用屏幕上的具体元素。需要至少提到一个："
            "屏幕元素（按钮 / 文字 / 标题 / 弹窗 / 图标 / 输入框…）、"
            "位置（顶部 / 底部 / 左侧 / 右侧…）、"
            "或带引号的 UI 文字（例如 「设置」）。"
            "光复述 step 文本不算 evidence — 必须是当前截图的实际可见内容。"
        )
    return True, ""


def validate_step_progress(decision: dict, prev_index: int, total_steps: int) -> tuple[bool, str]:
    """Validate the ``step_progress`` block of an LLM decision.

    Args:
        decision: Full decision dict from LLM.
        prev_index: ``current_step_index`` from the prior accepted decision
            (or 1 on the first call).
        total_steps: Number of steps in the Scenario (excluding Background).

    Returns ``(ok, reject_reason)``. On rejection, agent.py feeds
    ``reject_reason`` back to the LLM via the next ``decide()`` call's
    ``retry_feedback`` argument.
    """
    sp = decision.get("step_progress")
    if not isinstance(sp, dict):
        return False, "step_progress 字段缺失或格式错误。必须是 dict，含 current_step_index / current_step_status / evidence / fail_reason"

    # current_step_index
    cur = sp.get("current_step_index")
    if not isinstance(cur, int):
        return False, f"current_step_index 缺失或不是整数（收到 {cur!r}）。必须是 1-based int。"
    if cur < 1 or cur > total_steps:
        return False, (
            f"current_step_index={cur} 越界。合法范围 1..{total_steps}（Background 不计入）。"
        )
    # Monotonic: only +0 or +1 vs prev_index
    if cur < prev_index:
        return False, (
            f"current_step_index 倒退了（上一轮 {prev_index} → 这一轮 {cur}）。"
            "step 推进是单向的，已通过的 step 不能回头标 in_progress。"
        )
    if cur > prev_index + 1:
        return False, (
            f"current_step_index 跳跃了（上一轮 {prev_index} → 这一轮 {cur}）。"
            f"每轮最多前进一格，必须是 {prev_index}（继续当前 step）或 {prev_index + 1}（推进到下一 step）。"
            "**这是硬约束** — 即使屏幕看起来已经到了未来 step 的终态，也必须逐步推进。"
        )

    # current_step_status
    status = sp.get("current_step_status")
    if status not in ("in_progress", "pass", "fail"):
        return False, (
            f"current_step_status={status!r} 不合法。必须是 'in_progress' / 'pass' / 'fail' 之一。"
        )

    # evidence required for pass/fail
    if status in ("pass", "fail"):
        ev = sp.get("evidence", "")
        ok, reason = validate_evidence(ev if isinstance(ev, str) else "")
        if not ok:
            return False, f"current_step_status='{status}' 但 evidence 校验失败：{reason}"

    # fail_reason required when fail
    if status == "fail":
        fr = sp.get("fail_reason", "")
        if not isinstance(fr, str) or len(fr.strip()) < 10:
            return False, (
                "current_step_status='fail' 但 fail_reason 缺失或太短（< 10 字符）。"
                "需要说明为什么这一步不满足，引用 step 文本里具体哪条断言点未通过。"
            )

    # action required when in_progress
    if status == "in_progress":
        action = decision.get("action")
        if not isinstance(action, dict) or action.get("type") in (None, "done"):
            return False, (
                "current_step_status='in_progress' 但 action 缺失或是 done。"
                "继续推进当前 step 需要给出具体 action（tap / swipe / input / wait / ...）。"
            )

    return True, ""
