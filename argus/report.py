"""Test report generation — JSON and HTML formats."""

import base64
import json
import time
from datetime import datetime
from pathlib import Path

from .logger import get_logger

log = get_logger("report")


def _make_serializable(results: list[dict]) -> list[dict]:
    """Convert results to JSON-serializable form (strip binary screenshot data)."""
    out = []
    for r in results:
        entry = {k: v for k, v in r.items() if k != "steps_detail"}
        steps = []
        for s in r.get("steps_detail", []):
            step = {k: v for k, v in s.items() if k != "screenshot_png"}
            step["has_screenshot"] = s.get("screenshot_png") is not None
            steps.append(step)
        entry["steps_detail"] = steps
        # scenario_steps / step_status 保留原样（已是可序列化结构）
        out.append(entry)
    return out


# step 状态对应的图标 + 颜色 class
_STEP_STATUS_META = {
    "pass":    {"icon": "✓", "cls": "step-pass",    "label": "PASS"},
    "fail":    {"icon": "✗", "cls": "step-fail",    "label": "FAIL"},
    "skip":    {"icon": "−", "cls": "step-skip",    "label": "SKIP"},
    "pending": {"icon": "○", "cls": "step-pending", "label": "PENDING"},
}


# Healer verdict 对应的颜色 class + 中文标签
_HEAL_VERDICT_META = {
    "case_outdated":    {"cls": "heal-case-outdated",    "label": "CASE 已过期"},
    "app_bug":          {"cls": "heal-app-bug",          "label": "APP 缺陷"},
    "llm_misjudge":     {"cls": "heal-llm-misjudge",     "label": "LLM 误判"},
    "fixture_failure":  {"cls": "heal-fixture",          "label": "FIXTURE 未就位"},
    "flaky":            {"cls": "heal-flaky",            "label": "偶发失败"},
    "unknown":          {"cls": "heal-unknown",          "label": "未知"},
}


def _render_heal_report(report: dict | None) -> str:
    """渲染 Healer 根因分析报告。"""
    if not report:
        return ""
    verdict = report.get("verdict", "unknown")
    meta = _HEAL_VERDICT_META.get(verdict, _HEAL_VERDICT_META["unknown"])
    # confidence 来自 LLM JSON，插入 HTML 前必须转义
    confidence = _esc(report.get("confidence", "low"))
    summary = _esc(report.get("summary", ""))
    suggestion = _esc(report.get("suggestion", ""))
    case_fix = report.get("suggested_case_fix", "")

    fix_html = ""
    if case_fix:
        fix_html = (
            f'<details class="heal-fix"><summary>建议的 case 修订片段</summary>'
            f'<pre><code>{_esc(case_fix)}</code></pre></details>'
        )

    return (
        f'<div class="heal-report {meta["cls"]}">'
        f'<div class="heal-header">'
        f'<span class="heal-title">Healer 根因分析</span>'
        f'<span class="heal-verdict">{meta["label"]}</span>'
        f'<span class="heal-confidence">置信度 {confidence}</span>'
        f'</div>'
        f'<div class="heal-summary">{summary}</div>'
        f'{f"<div class=\"heal-suggestion\"><b>建议</b>：{suggestion}</div>" if suggestion else ""}'
        f'{fix_html}'
        f'</div>'
    )


def _render_scenario_steps(scenario_steps: list[str],
                            step_status: dict) -> str:
    """渲染 Scenario step 级状态列表（Playwright Test 风格树状）。"""
    if not scenario_steps:
        return ""

    rows = []
    # step_status 的 key 在反序列化后可能是 str（来自 JSON 报告往返）
    norm_status: dict = {}
    for k, v in (step_status or {}).items():
        try:
            norm_status[int(k)] = v
        except (TypeError, ValueError):
            continue

    for idx, step_text in enumerate(scenario_steps, start=1):
        status = norm_status.get(idx, "pending")
        meta = _STEP_STATUS_META.get(status, _STEP_STATUS_META["pending"])
        rows.append(
            f'<div class="bdd-step {meta["cls"]}">'
            f'<span class="bdd-step-icon">{meta["icon"]}</span>'
            f'<span class="bdd-step-num">{idx}.</span>'
            f'<span class="bdd-step-text">{_esc(step_text)}</span>'
            f'<span class="bdd-step-status">{meta["label"]}</span>'
            f'</div>'
        )

    # 汇总徽章
    counts = {k: 0 for k in _STEP_STATUS_META}
    for v in norm_status.values():
        if v in counts:
            counts[v] += 1
    badges = " &middot; ".join(
        f'<span class="bdd-badge bdd-badge-{k}">{counts[k]} {k}</span>'
        for k in ("pass", "fail", "skip", "pending") if counts[k] > 0
    )

    return (
        '<div class="bdd-steps">'
        f'<div class="bdd-steps-header">Scenario Steps ({len(scenario_steps)}) &nbsp; {badges}</div>'
        f'<div class="bdd-steps-list">{"".join(rows)}</div>'
        '</div>'
    )


def save_json(results: list[dict], output_path: str) -> str:
    """Save test results as JSON. Returns the path written."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "generated_at": datetime.now().isoformat(),
        "summary": _summary(results),
        "test_cases": _make_serializable(results),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log.info("JSON 报告已保存: %s", path)
    return str(path)


def save_html(results: list[dict], output_path: str) -> str:
    """Save test results as a self-contained HTML report. Returns the path written."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    summary = _summary(results)
    rows_html = []

    for i, r in enumerate(results, 1):
        if r["result"] == "pass":
            status_cls, status_text = "pass", "PASS"
        elif r["result"] == "skipped":
            status_cls, status_text = "skip", "SKIP"
        elif r["result"] == "timeout":
            status_cls, status_text = "fail", "TIMEOUT"
        else:
            status_cls, status_text = "fail", "FAIL"
        reason = r.get("reason", "")
        duration = f"{r.get('duration', 0):.1f}s"
        case_name = r.get("case", "")[:80]

        # Build a richer failure summary so the reader doesn't have to expand
        # 「步骤详情」 to figure out why a case failed / timed out:
        #   - pass: just show the agent's reason
        #   - fail / timeout: show reason + last step's observation + thinking
        fail_summary_html = ""
        if r["result"] != "pass":
            steps = r.get("steps_detail", []) or []
            last_step = steps[-1] if steps else None
            parts = []
            if reason:
                parts.append(f"<div><b>原因:</b> {_esc(reason)}</div>")
            if last_step:
                last_obs = last_step.get("observation", "")
                last_think = last_step.get("thinking", "")
                last_action = last_step.get("action") or {}
                last_n = last_step.get("step", "?")
                if last_obs:
                    parts.append(
                        f"<div><b>最后一步 (Step {last_n}) 观察:</b> {_esc(last_obs)}</div>"
                    )
                if last_think:
                    parts.append(
                        f"<div><b>最后一步思考:</b> {_esc(last_think)}</div>"
                    )
                if last_action:
                    parts.append(
                        f"<div><b>最后一步动作:</b> <code>"
                        f"{_esc(json.dumps(last_action, ensure_ascii=False))}</code></div>"
                    )
            if parts:
                fail_summary_html = (
                    '<div class="fail-summary">' + "".join(parts) + "</div>"
                )

        # Build steps detail
        steps_html = ""
        for s in r.get("steps_detail", []):
            step_num = s.get("step", "?")
            obs = _esc(s.get("observation", ""))
            think = _esc(s.get("thinking", ""))
            action = _esc(json.dumps(s.get("action") or {}, ensure_ascii=False))
            err = s.get("error")
            err_html = f'<div class="step-error">Error: {_esc(err)}</div>' if err else ""

            # Inline screenshot as base64 thumbnail
            screenshot_png = s.get("screenshot_png")
            img_html = ""
            if screenshot_png:
                b64 = base64.standard_b64encode(screenshot_png).decode()
                img_html = (
                    f'<details><summary>截图</summary>'
                    f'<img src="data:image/png;base64,{b64}" class="screenshot"/>'
                    f'</details>'
                )

            # step_progress 字段渲染（LLM 报的当前 step 状态 + evidence + fail_reason）
            # 这是 evidence 校验的真凭据，事后审查必看；之前漏渲染导致 HTML 报告里
            # 看不到 LLM 给的 evidence 文字，无法 audit 谎报
            sp = s.get("step_progress") or {}
            sp_html = ""
            if sp:
                sp_status = _esc(str(sp.get("current_step_status", "?")))
                # sp_idx 来自 LLM JSON，插入 HTML 前必须转义
                sp_idx = _esc(sp.get("current_step_index", "?"))
                sp_evidence = _esc(sp.get("evidence", "") or "")
                sp_fail = _esc(sp.get("fail_reason", "") or "")
                # status 上色
                status_color = {"pass": "#16a34a", "fail": "#dc2626",
                                "in_progress": "#d97706"}.get(
                    str(sp.get("current_step_status", "")).lower(), "#666")
                sp_html = (
                    f'<div style="border-left:3px solid {status_color};'
                    f'padding-left:10px;margin:8px 0;background:#fafafa;'
                    f'border-radius:4px;padding:8px 12px;">'
                    f'<div><b>Step {sp_idx} status:</b> '
                    f'<code style="color:{status_color}">{sp_status}</code></div>'
                )
                if sp_evidence:
                    sp_html += f'<div><b>evidence:</b> {sp_evidence}</div>'
                if sp_fail:
                    sp_html += f'<div><b>fail_reason:</b> {sp_fail}</div>'
                sp_html += '</div>'

            # rejected 字段：被 validator 拒绝时的反馈
            rej_html = ""
            if s.get("rejected"):
                rej_reason = _esc(s.get("reject_reason", ""))
                rej_html = (
                    f'<div style="border-left:3px solid #d97706;padding-left:10px;'
                    f'margin:8px 0;background:#fffbeb;border-radius:4px;'
                    f'padding:8px 12px;color:#92400e;font-size:12px;">'
                    f'⚠️ 被 validator 拒绝：{rej_reason}</div>'
                )

            steps_html += f"""
            <div class="step">
              <div class="step-header">Step {step_num}</div>
              <div class="step-body">
                {sp_html}
                {rej_html}
                <div><b>观察:</b> {obs}</div>
                <div><b>思考:</b> {think}</div>
                <div><b>动作:</b> <code>{action}</code></div>
                {err_html}
                {img_html}
              </div>
            </div>"""

        # Scenario step 级状态（BDD 视图）
        bdd_steps_html = _render_scenario_steps(
            r.get("scenario_steps") or [],
            r.get("step_status") or {},
        )
        # Healer 根因分析（仅 fail/timeout）
        heal_html = _render_heal_report(r.get("heal_report"))

        rows_html.append(f"""
        <div class="case">
          <div class="case-header {status_cls}">
            <span class="case-num">#{i}</span>
            <span class="case-status">{status_text}</span>
            <span class="case-name">{_esc(case_name)}</span>
            <span class="case-meta">{r.get('steps', 0)} agent steps &middot; {duration}</span>
          </div>
          <div class="case-reason">{_esc(reason)}</div>
          {fail_summary_html}
          {heal_html}
          {bdd_steps_html}
          <details><summary>Agent 决策步骤详情 (LLM 每一步的截图 + observation + action)</summary>
            <div class="steps">{steps_html}</div>
          </details>
        </div>""")

    # 头/尾模板分开，case rows 逐块流式写入文件 — 避免把所有 base64 截图
    # 拼成一个巨型字符串再落盘（大 run 内存翻倍）
    html_head = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Argus 测试报告</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
         background: #f5f5f5; color: #333; padding: 24px; }}
  h1 {{ font-size: 22px; margin-bottom: 16px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 24px; }}
  .summary .card {{ background: #fff; border-radius: 8px; padding: 16px 24px;
                    box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center; }}
  .summary .card .num {{ font-size: 28px; font-weight: 700; }}
  .summary .card .label {{ font-size: 13px; color: #888; }}
  .card.total .num {{ color: #333; }}
  .card.passed .num {{ color: #22c55e; }}
  .card.failed .num {{ color: #ef4444; }}
  .card.skipped .num {{ color: #9ca3af; }}
  .card.duration .num {{ color: #6366f1; font-size: 22px; }}
  .case {{ background: #fff; border-radius: 8px; margin-bottom: 12px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }}
  .case-header {{ display: flex; align-items: center; gap: 10px; padding: 12px 16px;
                  font-size: 14px; }}
  .case-header.pass {{ border-left: 4px solid #22c55e; }}
  .case-header.fail {{ border-left: 4px solid #ef4444; }}
  .case-header.skip {{ border-left: 4px solid #9ca3af; opacity: 0.65; }}
  .case-status {{ font-weight: 700; min-width: 60px; }}
  .pass .case-status {{ color: #22c55e; }}
  .fail .case-status {{ color: #ef4444; }}
  .skip .case-status {{ color: #9ca3af; }}
  .case-num {{ color: #aaa; min-width: 30px; }}
  .case-name {{ flex: 1; }}
  .case-meta {{ color: #999; font-size: 12px; white-space: nowrap; }}
  .case-reason {{ padding: 4px 16px 12px; font-size: 13px; color: #666; }}
  .fail-summary {{ background: #fef2f2; border-left: 3px solid #ef4444;
                    margin: 0 16px 12px; padding: 10px 12px; border-radius: 4px;
                    font-size: 12px; line-height: 1.6; }}
  .fail-summary > div {{ margin-bottom: 4px; }}
  .fail-summary code {{ background: #fff; padding: 2px 6px; border-radius: 3px;
                         font-size: 11px; word-break: break-all; }}
  details {{ padding: 0 16px 12px; }}
  summary {{ cursor: pointer; font-size: 13px; color: #666; user-select: none; }}
  .step {{ border-left: 2px solid #e0e0e0; margin: 8px 0; padding-left: 12px; }}
  .step-header {{ font-weight: 600; font-size: 13px; color: #555; }}
  .step-body {{ font-size: 13px; line-height: 1.6; }}
  .step-body code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px;
                     font-size: 12px; }}
  .step-error {{ color: #ef4444; font-weight: 600; }}
  .screenshot {{ max-width: 480px; margin-top: 8px; border: 1px solid #e0e0e0;
                 border-radius: 4px; }}
  /* BDD Scenario step 级渲染 */
  .bdd-steps {{ margin: 0 16px 12px; padding: 10px 12px;
                background: #fafafa; border-radius: 6px;
                border: 1px solid #ececec; font-size: 13px; }}
  .bdd-steps-header {{ font-weight: 600; color: #555; margin-bottom: 6px;
                       font-size: 12px; }}
  .bdd-badge {{ display: inline-block; padding: 1px 6px; border-radius: 8px;
                font-size: 11px; font-weight: 600; }}
  .bdd-badge-pass {{ background: #dcfce7; color: #166534; }}
  .bdd-badge-fail {{ background: #fee2e2; color: #991b1b; }}
  .bdd-badge-skip {{ background: #f3f4f6; color: #6b7280; }}
  .bdd-badge-pending {{ background: #fef3c7; color: #92400e; }}
  .bdd-steps-list {{ display: flex; flex-direction: column; gap: 2px; }}
  .bdd-step {{ display: flex; gap: 8px; align-items: baseline;
               padding: 4px 6px; border-radius: 4px; font-size: 12.5px;
               line-height: 1.5; }}
  .bdd-step-icon {{ font-weight: 700; min-width: 14px; text-align: center; }}
  .bdd-step-num {{ color: #999; min-width: 24px; }}
  .bdd-step-text {{ flex: 1; color: #333; font-family: ui-monospace,
                     "SF Mono", Monaco, "Cascadia Code", monospace; }}
  .bdd-step-status {{ font-size: 10px; font-weight: 700;
                       padding: 1px 6px; border-radius: 3px; }}
  .step-pass {{ background: #f0fdf4; }}
  .step-pass .bdd-step-icon, .step-pass .bdd-step-status {{ color: #16a34a; }}
  .step-fail {{ background: #fef2f2; }}
  .step-fail .bdd-step-icon, .step-fail .bdd-step-status {{ color: #dc2626; }}
  .step-skip {{ background: #fafafa; opacity: 0.7; }}
  .step-skip .bdd-step-icon, .step-skip .bdd-step-status {{ color: #9ca3af; }}
  .step-pending {{ background: #fffbeb; }}
  .step-pending .bdd-step-icon, .step-pending .bdd-step-status {{ color: #d97706; }}
  /* Healer 报告 */
  .heal-report {{ margin: 0 16px 12px; padding: 10px 12px;
                   border-radius: 6px; font-size: 12.5px; line-height: 1.5;
                   border-left: 4px solid #999; background: #fafafa; }}
  .heal-header {{ display: flex; align-items: center; gap: 10px;
                   margin-bottom: 6px; }}
  .heal-title {{ font-weight: 700; font-size: 12px; }}
  .heal-verdict {{ font-weight: 700; font-size: 11px; padding: 2px 8px;
                    border-radius: 3px; background: #fff; }}
  .heal-confidence {{ color: #666; font-size: 11px; }}
  .heal-summary {{ margin: 4px 0; color: #333; }}
  .heal-suggestion {{ margin: 6px 0; color: #444; }}
  .heal-fix {{ margin-top: 6px; padding: 0; }}
  .heal-fix summary {{ padding: 0; font-size: 11px; color: #666; }}
  .heal-fix pre {{ background: #1f2937; color: #e5e7eb; padding: 8px 12px;
                    border-radius: 4px; overflow-x: auto; font-size: 11px;
                    margin-top: 4px; }}
  .heal-case-outdated {{ border-left-color: #8b5cf6; }}
  .heal-case-outdated .heal-verdict {{ color: #8b5cf6; background: #f5f3ff; }}
  .heal-app-bug {{ border-left-color: #ef4444; }}
  .heal-app-bug .heal-verdict {{ color: #ef4444; background: #fef2f2; }}
  .heal-llm-misjudge {{ border-left-color: #f59e0b; }}
  .heal-llm-misjudge .heal-verdict {{ color: #d97706; background: #fffbeb; }}
  .heal-fixture {{ border-left-color: #3b82f6; }}
  .heal-fixture .heal-verdict {{ color: #2563eb; background: #eff6ff; }}
  .heal-flaky {{ border-left-color: #6b7280; }}
  .heal-flaky .heal-verdict {{ color: #4b5563; background: #f3f4f6; }}
  .heal-unknown {{ border-left-color: #999; }}
  .heal-unknown .heal-verdict {{ color: #6b7280; background: #f3f4f6; }}
  .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
  <h1>Argus 测试报告</h1>
  <div class="summary">
    <div class="card total"><div class="num">{summary['total']}</div><div class="label">总计</div></div>
    <div class="card passed"><div class="num">{summary['passed']}</div><div class="label">通过</div></div>
    <div class="card failed"><div class="num">{summary['failed']}</div><div class="label">失败</div></div>
    <div class="card skipped"><div class="num">{summary['skipped']}</div><div class="label">跳过</div></div>
    <div class="card duration"><div class="num">{_fmt_duration(summary['duration'])}</div><div class="label">总耗时</div></div>
  </div>
"""
    html_tail = f"""
  <div class="footer">Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by Argus</div>
</body>
</html>"""

    with path.open("w", encoding="utf-8") as f:
        f.write(html_head)
        for row in rows_html:
            f.write(row)
        f.write(html_tail)
    log.info("HTML 报告已保存: %s", path)
    return str(path)


def _summary(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["result"] == "pass")
    skipped = sum(1 for r in results if r["result"] == "skipped")
    total_duration = sum(r.get("duration", 0) for r in results)
    return {"total": total, "passed": passed, "skipped": skipped,
            "failed": total - passed - skipped,
            "duration": total_duration}


def _fmt_duration(seconds: float) -> str:
    """Format seconds to human-readable: "12.3s" / "2m 34s" / "1h 5m"."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def _esc(text) -> str:
    """Escape HTML special characters.

    None 安全：LLM 返回的 JSON 字段可能是 null（如 "observation": null），
    跑完一整轮后在 save_html 里崩掉会毁掉整份报告 — 这里兜底成空串。
    非字符串一律 str() 转换。"""
    if text is None:
        text = ""
    else:
        text = str(text)
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))
