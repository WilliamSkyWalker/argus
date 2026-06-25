"""Planner agent - 在 executor 启动前预先把 Scenario 拆成执行剧本。

输入：BDD .feature 渲染出的 case body 文本
输出：ScenarioPlan，含每个 step 的预期屏幕状态 + 动作提示

Planner 用一次 LLM 调用（轻量 prompt），结果作为 hint 注入 brain 的初始上下文，
帮 executor 在执行过程中保持 step 推进的连贯性，减少"看到屏幕一愣"的现象。

设计权衡：
- 不重新 plan UI 细节（避免与 executor 的视觉判断冲突）
- 只产出每 step 的"应该看到/应该做"语义级提示
- 失败时 graceful degrade — plan 为空也不影响 executor 工作
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .logger import get_logger

log = get_logger("planner")


_PLANNER_PROMPT = """你是一个测试 Planner Agent。下面给你一个 BDD Scenario 文本（Cucumber-Gherkin 语法）。

请：
1. 用一句话总结这个 Scenario 测什么（business value 视角）
2. 提取 Scenario 主体的 step 列表（**不含 Background** —— Background 是 fixture 不计入主 step）
3. 为每个 step 输出：
   - kind: given / when / then / and / but
   - text: step 原文（去掉 Given/When 等关键字前缀后的主体描述也可以保留完整）
   - intent: 用户/测试系统在这一步**意图**是什么（不是 UI 实现细节）
   - expected_state: 执行/验证后屏幕应处于什么状态（用户视角，1 句话）
   - action_hint: 若 kind 是 when/and 类的 action step，告诉 executor 具体怎么在屏幕上触发（如"在 Tab Bar 找 Latest 单击"）；若是 then 类的 assertion step，告诉 executor 怎么从当前截图判断（如"观察列表前几条标题是否与刷新前不同"）

返回严格 JSON（不要 markdown 代码块包裹），结构：
{
  "summary": "...",
  "steps": [
    {"index": 1, "kind": "given", "text": "...", "intent": "...", "expected_state": "...", "action_hint": "..."},
    ...
  ]
}

Scenario:
"""


@dataclass
class PlanStep:
    index: int
    kind: str
    text: str
    intent: str = ""
    expected_state: str = ""
    action_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index, "kind": self.kind, "text": self.text,
            "intent": self.intent, "expected_state": self.expected_state,
            "action_hint": self.action_hint,
        }


@dataclass
class ScenarioPlan:
    summary: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    raw_response: str = ""  # debug 用

    @property
    def is_empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_prompt_hint(self) -> str:
        """渲染成给 executor 看的执行 hint（注入 brain 上下文用）。"""
        if self.is_empty:
            return ""
        lines = [f"## Planner 预判：{self.summary}", "执行剧本（按序推进）："]
        for s in self.steps:
            lines.append(f"  {s.index}. [{s.kind.upper()}] {s.text}")
            if s.expected_state:
                lines.append(f"     预期态：{s.expected_state}")
            if s.action_hint:
                lines.append(f"     提示：{s.action_hint}")
        return "\n".join(lines)


def plan_scenario(test_case: str, llm_client, model: str,
                  timeout_s: float = 30.0, max_tokens: int = 8192) -> ScenarioPlan:
    """调一次 LLM，把 Scenario 文本预先 plan 成结构化剧本。

    失败时返回空 ScenarioPlan（不阻塞 executor）。
    max_tokens 由调用方从 config(LLM_MAX_TOKENS) 透传，与 brain 共用一个上限。"""
    try:
        response = llm_client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": _PLANNER_PROMPT + test_case}],
            response_format={"type": "json_object"},
            timeout=timeout_s,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        log.warning("Planner LLM 调用失败: %s", e)
        return ScenarioPlan()

    plan = _parse_plan_response(raw)
    plan.raw_response = raw
    if plan.is_empty:
        log.warning("Planner 返回空 plan，原始响应前 200 字: %s", raw[:200])
    else:
        log.info("Planner 生成 %d 步 plan: %s", len(plan.steps), plan.summary)
    return plan


_JSON_BLOCK = re.compile(r'\{.*\}', re.DOTALL)


def _parse_plan_response(raw: str) -> ScenarioPlan:
    """解析 Planner LLM 返回的 JSON，容错处理 markdown 代码块包裹/前后多余文本。"""
    if not raw:
        return ScenarioPlan()
    cleaned = raw.strip()
    # 去掉 ```json ... ``` 包裹
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned)
    # 兜底：找第一个 { 到最后一个 }
    m = _JSON_BLOCK.search(cleaned)
    if not m:
        return ScenarioPlan()
    try:
        data: Any = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log.debug("Plan JSON 解析失败: %s", e)
        return ScenarioPlan()
    if not isinstance(data, dict):
        return ScenarioPlan()

    summary = str(data.get("summary", "")).strip()
    steps_raw = data.get("steps", [])
    if not isinstance(steps_raw, list):
        return ScenarioPlan(summary=summary)

    steps: list[PlanStep] = []
    for i, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            continue
        steps.append(PlanStep(
            index=int(item.get("index", i)),
            kind=str(item.get("kind", "")).lower(),
            text=str(item.get("text", "")),
            intent=str(item.get("intent", "")),
            expected_state=str(item.get("expected_state", "")),
            action_hint=str(item.get("action_hint", "")),
        ))
    return ScenarioPlan(summary=summary, steps=steps)
