"""Healer agent - case fail 后分析根本原因 + 给修复建议。

输入：case body、step_status、最后几步 observation/thinking、最后一张截图
输出：HealReport（verdict + confidence + summary + suggestion）

Healer 用一次 LLM 调用（仅在 case fail/timeout 时），不影响 executor 主循环。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from .logger import get_logger

log = get_logger("healer")


# Healer verdict 分类
VERDICT_CASE_OUTDATED = "case_outdated"        # Case 描述与实际 App 行为不符（PRD 旧/功能砍掉）
VERDICT_APP_BUG = "app_bug"                    # App 实际行为偏离预期，是 App 缺陷
VERDICT_LLM_MISJUDGE = "llm_misjudge"          # LLM 看屏幕做了错误判断
VERDICT_FIXTURE_FAILURE = "fixture_failure"    # fixture 没就位（登录失败/网络异常等环境问题）
VERDICT_FLAKY = "flaky"                         # 偶发性失败（网络抖动、动画时序等）
VERDICT_UNKNOWN = "unknown"


_HEALER_PROMPT = """你是一个测试 Healer Agent。下面的测试用例失败了，请基于：
- Case 原文
- 每个 step 的最终状态（pass/fail/skip/pending）
- Agent 在最后几步的观察 + 思考
- 最后一张屏幕截图

判断**根本原因**属于以下哪一类：
1. **case_outdated** — Case 描述跟当前 App 实际行为不符（PRD 旧、功能砍掉、UI 改版未同步 case 等）
2. **app_bug** — App 实际行为偏离 PRD 预期，是 App 真实缺陷
3. **llm_misjudge** — App 行为本来正确，但 LLM 看屏幕判断错了
4. **fixture_failure** — fixture 未就位（未登录、断网、Onboarding 未完成等）
5. **flaky** — 偶发失败（动画时序、网络抖动）
6. **unknown** — 信息不足

返回严格 JSON（不要 markdown 包裹），结构：
{
  "verdict": "case_outdated" | "app_bug" | "llm_misjudge" | "fixture_failure" | "flaky" | "unknown",
  "confidence": "high" | "medium" | "low",
  "summary": "一句话说明你的判断依据",
  "suggestion": "对人类 QA 的建议（怎么修 case / 怎么报 bug / 怎么调 fixture）",
  "suggested_case_fix": "若 verdict=case_outdated，给出建议的 case 修订片段（Gherkin 格式）；其他 verdict 留空"
}

## 测试用例原文
"""


@dataclass
class HealReport:
    verdict: str = VERDICT_UNKNOWN
    confidence: str = "low"
    summary: str = ""
    suggestion: str = ""
    suggested_case_fix: str = ""
    raw_response: str = ""

    @property
    def is_empty(self) -> bool:
        return self.verdict == VERDICT_UNKNOWN and not self.summary

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "summary": self.summary,
            "suggestion": self.suggestion,
            "suggested_case_fix": self.suggested_case_fix,
        }


def analyze_failure(
    test_case: str,
    scenario_steps: list[str],
    step_status: dict[int, str],
    steps_detail: list[dict],
    last_screenshot_png: bytes | None,
    llm_client,
    model: str,
    timeout_s: float = 45.0,
) -> HealReport:
    """调一次 LLM 分析 case 失败根因。

    失败时返回空 HealReport（verdict=unknown）。"""

    # 构造给 LLM 的上下文
    step_status_lines = []
    for i, step_text in enumerate(scenario_steps, start=1):
        status = step_status.get(i) or step_status.get(str(i), "pending")
        step_status_lines.append(f"  {i}. [{status.upper()}] {step_text}")
    step_status_text = "\n".join(step_status_lines) or "（无法解析 step）"

    # 最后 3 步的 observation/thinking/action 给 LLM
    last_agent_steps = steps_detail[-3:] if steps_detail else []
    last_agent_text_parts = []
    for s in last_agent_steps:
        n = s.get("step", "?")
        obs = s.get("observation", "")
        think = s.get("thinking", "")
        act = s.get("action") or {}
        last_agent_text_parts.append(
            f"### Agent Step {n}\n"
            f"observation: {obs}\n"
            f"thinking: {think}\n"
            f"action: {json.dumps(act, ensure_ascii=False)}"
        )
    last_agent_text = "\n\n".join(last_agent_text_parts) or "（无 agent step 信息）"

    user_text = (
        _HEALER_PROMPT +
        test_case +
        "\n\n## Step 最终状态\n" + step_status_text +
        "\n\n## Agent 最后几步\n" + last_agent_text
    )

    user_content: list[dict] = [{"type": "text", "text": user_text}]

    if last_screenshot_png:
        b64 = base64.standard_b64encode(last_screenshot_png).decode()
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    try:
        response = llm_client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": user_content}],
            timeout=timeout_s,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        log.warning("Healer LLM 调用失败: %s", e)
        return HealReport()

    report = _parse_heal_response(raw)
    report.raw_response = raw
    if report.is_empty:
        log.warning("Healer 返回空报告，原始响应前 200 字: %s", raw[:200])
    else:
        log.info("Healer verdict=%s confidence=%s | %s",
                 report.verdict, report.confidence, report.summary[:80])
    return report


_JSON_BLOCK = re.compile(r'\{.*\}', re.DOTALL)
_VALID_VERDICTS = {
    VERDICT_CASE_OUTDATED, VERDICT_APP_BUG, VERDICT_LLM_MISJUDGE,
    VERDICT_FIXTURE_FAILURE, VERDICT_FLAKY, VERDICT_UNKNOWN,
}
_VALID_CONFIDENCE = {"high", "medium", "low"}


def _parse_heal_response(raw: str) -> HealReport:
    """解析 Healer LLM 返回的 JSON。"""
    if not raw:
        return HealReport()
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned)
    m = _JSON_BLOCK.search(cleaned)
    if not m:
        return HealReport()
    try:
        data: Any = json.loads(m.group(0))
    except json.JSONDecodeError:
        return HealReport()
    if not isinstance(data, dict):
        return HealReport()

    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in _VALID_VERDICTS:
        verdict = VERDICT_UNKNOWN
    confidence = str(data.get("confidence", "")).lower().strip()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"

    return HealReport(
        verdict=verdict,
        confidence=confidence,
        summary=str(data.get("summary", "")).strip(),
        suggestion=str(data.get("suggestion", "")).strip(),
        suggested_case_fix=str(data.get("suggested_case_fix", "")).strip(),
    )
