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
   current_step_index == 当前待执行 step（指针推进由 agent.py 负责，LLM
   不许自行 +1，否则会漏执行当前 step）、current_step_status legality,
   evidence presence when status=pass/fail, fail_reason presence
   when status=fail.
"""

from __future__ import annotations

import re

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
        prev_index: 当前待执行 step 的序号（agent.py 维护；step pass 后由
            框架推进指针，LLM 报的 index 必须等于它）。
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
    # 必须等于当前待执行 step。指针推进由 agent.py 负责（pass 后 +1），
    # LLM 不许自行 +1 —— 否则 pending step 还没执行就能 pass 下一个 step，
    # case 会带着未执行的 step 假完成（跳步洞）。
    if cur < prev_index:
        return False, (
            f"current_step_index 倒退了（上一轮 {prev_index} → 这一轮 {cur}）。"
            "step 推进是单向的，已通过的 step 不能回头标 in_progress。"
        )
    if cur > prev_index:
        return False, (
            f"current_step_index 跳跃了（当前待执行 step 是 {prev_index}，你报了 {cur}）。"
            f"current_step_index 必须等于当前待执行 step {prev_index} —— "
            "step 指针的推进由 agent 框架完成，你只负责报告当前 step 的状态。"
            "**这是硬约束** — 即使屏幕看起来已经到了未来 step 的终态，也必须先把当前 step 验完。"
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


# ── 连续断言合并（Phase 3）的批量校验：把逐步硬墙逐条套用，并加合并专属的防偷懒闸 ──

# 负向断言标记（"不展示 / 没有 X" 一类）——判 pass 时 evidence 必须写明"看过、确认不存在"。
_NEG_MARKERS = ("不展示", "不显示", "没有", "不应", "不得", "不出现", "未出现",
                "不存在", "不再", "禁止", "无 ", "不含", "不会")
_ABSENCE_EVIDENCE = ("未发现", "没有", "不存在", "未出现", "查看", "查了", "检查",
                     "找不到", "不见", "无此", "均无", "确认无", "扫", "遍历")


def _is_negative_assertion(text: str) -> bool:
    return any(m in (text or "") for m in _NEG_MARKERS)


def _norm_evidence(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def validate_assertion_batch(results: list, assertions: list) -> tuple[bool, str]:
    """校验合并断言（Phase 3）的一批逐条结果。任一条不达标 → 打回整批（reject 重判）。

    Args:
        results: LLM 回的 [{id, verdict, evidence, where, fail_reason}, ...]
        assertions: [{index, text}, ...]（这批断言的 step 序号 + 原文）

    逐条套用 validate_evidence 硬墙，另加合并专属红线：where 必填、负向断言 pass 需写明
    "看过未发现"、跨条 evidence 去重（防雷同/模板化偷懒）。Returns ``(ok, reject_reason)``。
    """
    if not isinstance(results, list) or not results:
        return False, "results 缺失或为空；每条断言都要回一条 {id, verdict, evidence, where}。"
    by_id: dict[int, dict] = {}
    for r in results:
        if not isinstance(r, dict) or "id" not in r:
            return False, "results 里有条目缺 id；每条必须带 id（=step 序号）。"
        try:
            by_id[int(r["id"])] = r
        except (TypeError, ValueError):
            return False, f"results 里 id={r.get('id')!r} 不是整数。"

    expected = [a["index"] for a in assertions]
    text_by_id = {a["index"]: a["text"] for a in assertions}
    missing = [i for i in expected if i not in by_id]
    if missing:
        return False, f"漏判 step {missing}；这批每条断言都要各回一次 verdict，一条不能少。"

    seen: list[tuple[int, str]] = []
    for idx in expected:
        r = by_id[idx]
        verdict = r.get("verdict")
        if verdict not in ("pass", "fail"):
            return False, f"step {idx} 的 verdict={verdict!r} 不合法，必须是 'pass' / 'fail'。"
        ev = r.get("evidence", "") if isinstance(r.get("evidence"), str) else ""
        ok, reason = validate_evidence(ev)
        if not ok:
            return False, f"step {idx} evidence 校验失败：{reason}"
        where = r.get("where", "")
        if not isinstance(where, str) or len(where.strip()) < 2:
            return False, f"step {idx} 缺 where（该证据在屏幕上的位置）——合并判定必须逐条给位置。"
        if verdict == "fail":
            fr = r.get("fail_reason", "")
            if not isinstance(fr, str) or len(fr.strip()) < 10:
                return False, f"step {idx} verdict=fail 但 fail_reason 缺失或太短（< 10 字符）。"
        # 负向断言判 pass：evidence 必须体现"看过、确认不存在"，不能默认没有就过
        if verdict == "pass" and _is_negative_assertion(text_by_id.get(idx, "")):
            if not any(m in ev for m in _ABSENCE_EVIDENCE):
                return False, (
                    f"step {idx} 是负向断言（「{text_by_id.get(idx, '')[:24]}」）判 pass，"
                    "evidence 必须写明「查看了<区域>、未发现 X」——明确说你看了哪里、确认它不存在，"
                    "不能默认没有就 pass。"
                )
        # 去重闸：跨条雷同/模板化 evidence
        ne = _norm_evidence(ev)
        for prev_idx, prev_ne in seen:
            if ne == prev_ne or (len(ne) > 20 and (ne in prev_ne or prev_ne in ne)):
                return False, (
                    f"step {idx} 与 step {prev_idx} 的 evidence 雷同/模板化——"
                    "不同断言要引用各自不同的屏上元素/文字/位置，别套用同一句证据。"
                )
        seen.append((idx, ne))
    return True, ""
