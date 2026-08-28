"""Main agent loop — ties eyes, brain, and hands together via Platform.

Step-driven model (修正版档 3 — 「看全局，禁跳跃」):

  Outer loop iterates over Scenario steps. For each step we run an inner
  loop of LLM sub-actions (capped at ``PER_STEP_SUB_ACTION_LIMIT``) until
  the LLM declares ``current_step_status = pass``. On ``fail`` we abort
  the whole Scenario (Cucumber semantics).

  The LLM always sees the **full** step list (for narrative context) but
  every decision passes through ``step_validator``:
    - current_step_index must equal the pending step (指针推进由 agent 完成，
      LLM 自行 +1 会被 reject —— 防止当前 step 未执行就 pass 下一 step)
    - evidence required + must reference concrete screen elements when
      status is pass/fail
    - fail_reason required when status=fail
    - action required when status=in_progress

  Rejected decisions don't consume a sub-action slot — the reject reason
  is fed back via ``retry_feedback`` so the LLM self-corrects. A per-step
  ``MAX_REJECTS`` guards against infinite reject loops.
"""

import io
import re
import time

from PIL import Image

from .brain import Brain
from .locator import ElementLocator
from .logger import get_logger
from .planner import plan_scenario
from .platforms import create_platform
from .probes import (MAX_ATTEMPTS_PER_STEP, MIN_POLL_INTERVAL_S, ProbeContext,
                     ProbeRunner, ProbeSpec, summarize_data)
from .probes.spec import parse_directive_line
from .settle import wait_settled, sample_then_frames
from .skills import SkillContext, create_pipeline, run_pipeline
from .step_validator import validate_step_progress, validate_assertion_batch

log = get_logger("agent")

ACTION_MAX_RETRIES = 2

# Max LLM sub-actions per Gherkin step before we force a step timeout.
# -1 = 禁用该上限（不按 sub-action 次数掐断 step）。禁用后由外层 self.max_steps 兜底防失控，
# 配合 no_effect 重试阶梯 + MAX_REJECTS_PER_STEP 控制无效重试；正数则恢复硬上限。
PER_STEP_SUB_ACTION_LIMIT = -1

# Max consecutive validator rejects within a single step. If the LLM
# can't produce a valid step_progress block after this many tries we
# fail the step with a clear reason rather than burning all sub-actions.
MAX_REJECTS_PER_STEP = 3

# 连续多少个 turn「未推进到下一 Gherkin step」就判该 step fail（每推进一步即重置）。
# 这是禁用 PER_STEP_SUB_ACTION_LIMIT / AGENT_MAX_STEPS 后的主收敛保护：
# 正常长 step（如多屏滚动，每次都在干活但只在末尾 pass）有足够 turn 余量；
# 真卡死（不停动作却永不 pass）会在这里被掐断。整 case 上限 ≈ n_steps × 本值。
MAX_TURNS_WITHOUT_PROGRESS = 15

# 单个 step 内最多用 元素定位兜底重定位几次（超过则回落到网格兜底，防死循环）。
MAX_LOCATE_PER_STEP = 2

# 分层执行（split_act_check）：操作步 元素定位失败/执行异常连续几次就逃生回大 LLM。
ESCAPE_ACTION_FAILS = 2

# 合并断言（Phase 3）疑似被弹窗挡住时，最多"关弹窗+重判"几次（防叠层弹窗/防死循环）。
MAX_POPUP_DISMISS = 2

# 断言型 step 关键字（触发多帧时间窗断言，见 #4）。And/But 的实际类别继承前一 primary。
_ASSERT_KEYWORDS = ("Then", "But", "那么", "但是")
_ACTION_KEYWORDS = ("When", "Given", "当", "假如", "如果", "前提")

# 匹配 Gherkin step 行（Given/When/Then/And/But + 后续文本）
_STEP_LINE_RE = re.compile(r'^\s*(Given|When|Then|And|But)\s+(.+)$')

# case body 头行：`### TC-XXX  <名字>` —— 取 case id 给 probe 上下文用
_CASE_ID_RE = re.compile(r'^\s*###\s+(\S+)')


def _extract_steps_and_probes(case_text: str) -> tuple[list[str], dict[int, ProbeSpec]]:
    """从 case body 提取 Scenario step 列表 + step 级 probe 声明（不含 Background）。

    匹配 argus.gherkin.render_case 输出格式：
        - **Steps**:
          Given xxx
          When xxx
          Then xxx
          # argus-probe: analytics check=首页曝光     ← 绑定上面那个 Then
          And xxx
          But xxx

    probe 声明绑定「它上面最近的那个 step」，返回 {step_index(1-based): ProbeSpec}。
    一个 step 只挂一条：多个断言请拆成多个 Then 各挂一条（也符合用例约定里
    「Then 拆成逐条可验证 bullet」），或用数组参数一次传多个事件。
    """
    lines = case_text.splitlines()
    in_steps = False
    steps: list[str] = []
    probes: dict[int, ProbeSpec] = {}
    for line in lines:
        stripped = line.strip()
        if stripped == "- **Steps**:" or stripped.startswith("- **Steps**:"):
            in_steps = True
            continue
        if in_steps:
            # 遇到下一个 markdown 字段就结束
            if stripped.startswith("- **") and "**:" in stripped:
                break
            if _STEP_LINE_RE.match(line):
                steps.append(stripped)
                continue
            spec = parse_directive_line(line)
            if spec is None:
                continue
            if not steps:
                log.warning("probe 声明出现在第一个 step 之前，忽略: %s", stripped)
                continue
            idx = len(steps)
            if idx in probes:
                log.warning("step %d 已挂 probe「%s」，被后一条「%s」覆盖"
                            "（一个 step 只支持一条 probe 声明）",
                            idx, probes[idx].summary(), spec.summary())
            probes[idx] = spec
    return steps, probes


def _extract_scenario_steps(case_text: str) -> list[str]:
    """只要 step 列表时的便捷封装（见 _extract_steps_and_probes）。"""
    return _extract_steps_and_probes(case_text)[0]


def _classify_assertion_steps(scenario_steps: list[str]) -> list[bool]:
    """判定每个 step 是否为断言型（Then/But，And/But 继承上一个 primary 关键字）。

    断言型 step 触发多帧时间窗断言（#4）——瞬态 UI（toast/banner）可能在动作后
    出现又消失，单帧会漏。返回与 scenario_steps 等长的 bool 列表。
    """
    flags: list[bool] = []
    prev_is_assert = False
    for step in scenario_steps:
        head = step.split(None, 1)[0] if step.split() else ""
        if head in _ASSERT_KEYWORDS:
            prev_is_assert = True
        elif head in _ACTION_KEYWORDS:
            prev_is_assert = False
        # And / 而且 / 并且 / 同时：延续上一 primary 类别（prev_is_assert 不变）
        flags.append(prev_is_assert)
    return flags


def _consecutive_assert_block(flags: list[bool], start_idx: int) -> tuple[int, int] | None:
    """从 start_idx（1-based）起，返回极大连续断言块 (i, j) 闭区间（1-based）。

    连续断言块 = 相邻且中间无操作步的 Then/And/But——它们判的是同一结果屏，可合并
    成一次调用（Phase 3）。start_idx 本身不是断言步则返回 None。
    """
    n = len(flags)
    if not (1 <= start_idx <= n) or not flags[start_idx - 1]:
        return None
    end = start_idx
    while end < n and flags[end]:   # flags[end]（0-indexed）= step end+1 是否断言
        end += 1
    return (start_idx, end)


class Agent:
    def __init__(self, config: dict | None = None):
        log.info("Agent.__init__ 开始")

        from .config import load_config
        cfg = config or load_config()
        self.cfg = cfg   # probe 上下文要用（包名 / 设备 / 平台）
        log.info("配置已加载: platform=%s", cfg.get("platform", "ios"))

        platform_name = cfg.get("platform", "ios")
        log.info("创建平台: %s", platform_name)
        self.platform = create_platform(platform_name, cfg)
        log.info("平台已创建, 开始 setup...")

        self.platform.setup(cfg)
        log.info("平台 setup 完成")

        # MCP client registry — 若 .argus/mcp_clients.json 存在则加载。文件含 token
        # 已被 .gitignore，所以不会误启用到 CI；本地开发要用时手动放 example 复制版。
        mcp_registry = None
        try:
            from .mcp.client import MCPRegistry
            candidate_registry = MCPRegistry.from_config()
            if candidate_registry.servers:
                mcp_registry = candidate_registry
                log.info("MCP registry: %d server(s) → %s",
                         len(mcp_registry.servers),
                         list(mcp_registry.servers.keys()))
        except Exception as e:
            log.warning("MCP registry 加载失败 (continuing without MCP): %s", e)

        log.info("创建 Brain (LLM client)...")
        self.brain = Brain(cfg["llm"], platform=self.platform,
                           mcp_registry=mcp_registry)
        log.info("Brain 创建完成, model=%s", cfg["llm"].get("model", "?"))

        # 分级模型：planner 用自己的 model（缺省回落 brain model）
        self.planner_model = cfg["llm"].get("planner_model") or self.brain.model
        # 元素定位兜底（LLM_MODEL_LOCATOR 为空则 disabled）
        self.locator = ElementLocator(cfg["llm"].get("locator"))
        if self.locator.enabled:
            log.info("元素定位兜底已启用: model=%s", self.locator.model)

        self.max_steps = cfg["agent"]["max_steps"]
        self.step_delay = cfg["agent"]["step_delay"]
        self.locate_retry = int(cfg["agent"].get("locate_retry", 2))
        self.assert_burst_frames = int(cfg["agent"].get("assert_burst_frames", 1))
        # settle 闸（Phase 1）：截图前等屏幕稳定再决策/采样，零 LLM。
        self.settle_enabled = bool(cfg["agent"].get("settle_enabled", True))
        self.settle_timeout = float(cfg["agent"].get("settle_timeout", 6.0))
        self.settle_interval = float(cfg["agent"].get("settle_interval", 0.3))
        self.settle_stable_frames = int(cfg["agent"].get("settle_stable_frames", 2))
        # wait_for（Phase 2）：per-step 墙钟等待预算；等待轮不计入 no-progress，超预算才收敛。
        self.wait_max_s = float(cfg["agent"].get("wait_max_s", 45.0))
        # 连续断言合并（Phase 3，默认关）：连续同屏 Then/And 合成 1 次调用逐条判。
        self.merge_asserts = bool(cfg["agent"].get("merge_asserts", False))
        if self.merge_asserts:
            log.info("连续断言合并已启用（Phase 3）")
        if self.settle_enabled:
            log.info("settle 闸已启用: timeout=%.1fs interval=%.2fs stable=%d",
                     self.settle_timeout, self.settle_interval, self.settle_stable_frames)
        # 分层执行：操作步走元素定位、检查步走 LLM（依赖元素定位找 tap/input 目标）
        self.split_act_check = bool(cfg["agent"].get("split_act_check", False))
        if self.split_act_check:
            if self.locator.enabled:
                log.info("分层执行已启用: 操作步走元素定位直接执行, 检查步走 LLM")
            else:
                log.warning("分层执行已开但 元素定位未启用(LLM_MODEL_LOCATOR 空): "
                            "tap/input 操作步将无法定位、直接逃生回大 LLM")
        log.info("max_steps=%d, step_delay=%.1f", self.max_steps, self.step_delay)

        log.info("创建 skills pipeline...")
        self.skills_pipeline = create_pipeline(cfg.get("skills"))
        log.info("skills pipeline 创建完成: %s", [s.name for s in self.skills_pipeline])

        # 非视觉断言插件（埋点/后端落库/上报日志）。没有 .argus/probes.json 时
        # registry 为空 —— 只有用例真声明了 `# argus-probe:` 才会用到，行为零变化。
        self.probes = ProbeRunner(cfg)
        # all（默认）/ skip（probe step 标 skip 直接推进）。only 是 cli 层的 case 级
        # 筛选（只跑含 probe 的 case），到了这里跟 all 一样正常跑。
        self.probes_mode = (cfg.get("probes") or {}).get("mode") or "all"
        if self.probes_mode == "skip":
            log.warning("--skip-probes: 非视觉断言 step 一律跳过（标 skip 不判定）")
        # cli 可以往这里塞跑测级上下文（run_id / target / 绑定的账号 / 产物目录），
        # 供 probe 框出「本次跑测产生的数据」。默认空，probe 拿不到就自己兜。
        self.probe_context: dict = {}

        log.info("Agent.__init__ 完成")

    def run(self, test_case: str) -> dict:
        """Execute a test case with step-driven loop + validator gating."""
        self.brain.reset()
        log.info("=" * 60)
        log.info("测试用例: %s", test_case)
        log.info("=" * 60)

        # 提取 Scenario 的 step 列表（用于 step 级报告 + LLM narrative）
        # 顺带取出 step 级 probe 声明：挂了 probe 的 step 不进 LLM，走插件判定
        scenario_steps, probes_by_step = _extract_steps_and_probes(test_case)
        if not scenario_steps:
            # inline / 无 Steps 段的 case：合成单 step，否则 n_steps=0 会让
            # validator 拒绝一切 index（合法范围 1..0），case 必 fail
            summary = next((ln.strip() for ln in test_case.splitlines() if ln.strip()),
                           test_case.strip())
            scenario_steps = [summary[:200]]
            log.info("case 无 Steps 段，合成单 step: %s", scenario_steps[0])
        n_steps = len(scenario_steps)
        step_status: dict[int, str] = {i: "pending" for i in range(1, n_steps + 1)}
        assertion_flags = _classify_assertion_steps(scenario_steps)
        log.info("Scenario steps 提取: %d 步", n_steps)

        # 参考图（#5）：case 里声明的 `- **Ref**:` 设计稿，供断言型 step 视觉走查对比
        reference_images = self._load_reference_images(test_case)

        # ── Planner: 跑一次 LLM 把 case 拆成执行剧本，作为 hint 注入 brain ──
        # graceful: plan 失败为空，不阻塞 executor。
        plan_hints_by_idx: dict[int, str] = {}
        plan_acts_by_idx: dict[int, dict] = {}   # 分层执行:操作步的结构化动作
        if n_steps > 0:
            try:
                t_plan = time.time()
                plan = plan_scenario(test_case, self.brain.client, self.planner_model,
                                     max_tokens=self.brain.max_tokens)
                log.info("Planner 完成 (%.2fs): %s", time.time() - t_plan, plan.summary)
                for s in plan.steps:
                    hint_parts = []
                    if s.intent:
                        hint_parts.append(f"intent: {s.intent}")
                    if s.expected_state:
                        hint_parts.append(f"expected_state: {s.expected_state}")
                    if s.action_hint:
                        hint_parts.append(f"action_hint: {s.action_hint}")
                    if hint_parts and 1 <= s.index <= n_steps:
                        plan_hints_by_idx[s.index] = "\n".join(hint_parts)
                    if s.act and 1 <= s.index <= n_steps:
                        plan_acts_by_idx[s.index] = s.act
            except Exception as e:
                log.warning("Planner 异常 (graceful skip): %s", e)

        # ── Step-driven 主循环 state ──
        current_step_index = 1
        sub_actions_in_step = 0
        rejects_in_step = 0
        completed_evidence: list[str] = []  # 一项一个已通过 step 的 evidence
        retry_feedback = ""                 # validator reject 时塞给下一次 decide

        steps_detail: list[dict] = []
        start_time = time.time()
        prev_raw_image = None
        prev_ime_visible = False  # 上一回合软键盘是否可见（检测 tap 是否唤起键盘=聚焦输入框）
        consec_no_effect = 0      # 连续 no_effect 次数（定位不准的信号）
        turns_without_progress = 0  # 自上次推进到下一 step 以来累计的 turn（推进即清零）
        wait_elapsed_in_step = 0.0  # 本 step 累计"主动等待"墙钟秒（Phase 2；推进即清零）
        locate_attempts_in_step = 0  # 本 step 已用 元素定位兜底几次（cap 见 MAX_LOCATE_PER_STEP）
        act_fails = 0  # 分层执行:本操作步连续失败次数（达 ESCAPE_ACTION_FAILS 逃生回大 LLM）
        batch_done = False  # 分层执行:本操作步是否已批量执行过(执行后→下一轮走 brain 验证)
        step_started_at = start_time  # 本 step 开始墙钟（probe 的等数据预算从这里算）
        probe_attempts_in_step = 0    # 本 step 已查了几次 probe（推进即清零）

        probe_skipped: list[int] = []  # --skip-probes 下被跳过的 step（进 reason/报告）
        self._probe_skipped_steps = probe_skipped   # _build_result 读它给 reason 加注

        # probe（非视觉断言）：本 case 开跑，给插件 setup 的机会（懒调，首次 check 前）
        if probes_by_step and self.probes_mode == "skip":
            log.warning("本 case 有 %d 个非视觉断言 step，按 --skip-probes 全部跳过: %s",
                        len(probes_by_step),
                        {i: s.summary() for i, s in probes_by_step.items()})
        elif probes_by_step:
            log.info("本 case 有 %d 个非视觉断言 step 走 probe: %s",
                     len(probes_by_step),
                     {i: s.summary() for i, s in probes_by_step.items()})
            try:
                self.probes.begin_case(self._probe_ctx(
                    scenario_steps, step_index=0, step_text="",
                    case_started_at=start_time, step_started_at=start_time,
                    attempt=0, elapsed_s=0.0, timeout_s=0.0, case_text=test_case))
            except Exception as e:
                log.warning("probe begin_case 异常（继续跑）: %s", e)

        # turn = LLM 调用次数（含被 reject 的、含成功的 sub-action）。
        # 主收敛：per-step 的 MAX_TURNS_WITHOUT_PROGRESS（连续无推进即 fail）。
        # self.max_steps 为可选绝对兜底：AGENT_MAX_STEPS<=0 时禁用（仅靠 per-step 上限收敛）。
        turn = 0
        while True:
            turn += 1
            # 绝对兜底（默认禁用，AGENT_MAX_STEPS>0 才作硬顶）
            if self.max_steps > 0 and turn > self.max_steps:
                log.warning("达到外层 max_steps 限制 (%d)，测试未完成", self.max_steps)
                for i in step_status:
                    if step_status[i] == "pending":
                        step_status[i] = "skip"
                return self._build_result(
                    "timeout", f"max_steps {self.max_steps} reached",
                    turn, start_time, steps_detail, scenario_steps, step_status,
                )
            turn_start = time.time()
            step_record = {
                "turn": turn,
                "gherkin_step_index": current_step_index,
                "screenshot_png": None, "action": None,
                "observation": "", "thinking": "", "error": None,
                "step_progress": None,
                "rejected": False, "reject_reason": "",
            }

            # ── 0. 连续无推进上限（主收敛：到达即 fail；推进到下一 step 会重置计数）──
            if turns_without_progress >= MAX_TURNS_WITHOUT_PROGRESS:
                msg = (f"Step {current_step_index} 连续 {MAX_TURNS_WITHOUT_PROGRESS} 个 turn"
                       f" 未推进到下一步，标 fail 并终止 scenario")
                log.warning(msg)
                step_status[current_step_index] = "fail"
                for i in range(current_step_index + 1, n_steps + 1):
                    step_status[i] = "skip"
                return self._build_result(
                    "fail", f"step {current_step_index} no-progress: {msg}",
                    turn, start_time, steps_detail, scenario_steps, step_status,
                )

            # ── 0b. Per-step sub-action 上限保护（PER_STEP_SUB_ACTION_LIMIT < 0 时禁用）──
            if PER_STEP_SUB_ACTION_LIMIT >= 0 and sub_actions_in_step >= PER_STEP_SUB_ACTION_LIMIT:
                msg = (f"Step {current_step_index} 超过 {PER_STEP_SUB_ACTION_LIMIT} 次 sub-action"
                       f" 仍未推进，标 fail 并终止 scenario")
                log.warning(msg)
                step_status[current_step_index] = "fail"
                for i in range(current_step_index + 1, n_steps + 1):
                    step_status[i] = "skip"
                return self._build_result(
                    "fail", f"step {current_step_index} timeout: {msg}",
                    turn, start_time, steps_detail, scenario_steps, step_status,
                )

            if rejects_in_step >= MAX_REJECTS_PER_STEP:
                msg = (f"Step {current_step_index} 连续 {MAX_REJECTS_PER_STEP} 次 LLM 输出被 validator 拒绝，"
                       f"标 fail 并终止 scenario。最后一次 reject 理由：{retry_feedback}")
                log.warning(msg)
                step_status[current_step_index] = "fail"
                for i in range(current_step_index + 1, n_steps + 1):
                    step_status[i] = "skip"
                return self._build_result(
                    "fail", msg, turn, start_time, steps_detail, scenario_steps, step_status,
                )

            # 本 turn 先计入"未推进"；若稍后 status==pass 推进到下一 step，会在推进分支清零
            turns_without_progress += 1

            # ── 0c. 非视觉断言 step（埋点等）：**不进 LLM**，直接跑 probe 插件 ──
            # verdict 来自代码层，所以 brain 那套「不可视断言禁 PASS」的反谎报硬墙
            # 完全不用动 —— 这些 step LLM 根本看不到。查不到数据时按 probe 给的节奏
            # 重试，重试轮不计入 no-progress（同 wait 动作语义），预算耗尽才判 fail。
            probe_spec = probes_by_step.get(current_step_index)
            if probe_spec is not None:
                if self.probes_mode == "skip":
                    kind, payload = self._skip_probe_step(
                        probe_spec, current_step_index, step_record, turn)
                else:
                    kind, payload = self._run_probe_step(
                        probe_spec, current_step_index, scenario_steps, test_case,
                        attempt=probe_attempts_in_step + 1,
                        case_started_at=start_time, step_started_at=step_started_at,
                        step_record=step_record, turn=turn,
                    )
                probe_attempts_in_step += 1
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)

                if kind in ("pass", "skip"):
                    ev = str(payload)
                    while len(completed_evidence) < current_step_index:
                        completed_evidence.append("")
                    completed_evidence[current_step_index - 1] = ev
                    if kind == "skip":
                        step_status[current_step_index] = "skip"
                        probe_skipped.append(current_step_index)
                    else:
                        step_status[current_step_index] = "pass"
                        log.info("[Turn %d] ✅ Step %d PASS (probe %s): %s",
                                 turn, current_step_index, probe_spec.name, ev)
                    if current_step_index >= n_steps:
                        return self._build_result(
                            "pass", "all steps passed",
                            turn, start_time, steps_detail, scenario_steps, step_status,
                        )
                    current_step_index += 1
                    sub_actions_in_step = 0
                    rejects_in_step = 0
                    turns_without_progress = 0
                    wait_elapsed_in_step = 0.0
                    locate_attempts_in_step = 0
                    act_fails = 0
                    batch_done = False
                    probe_attempts_in_step = 0
                    step_started_at = time.time()
                    time.sleep(self.step_delay)
                    continue

                if kind == "fail":
                    step_status[current_step_index] = "fail"
                    for i in range(current_step_index + 1, n_steps + 1):
                        step_status[i] = "skip"
                    log.warning("[Turn %d] ❌ Step %d FAIL (probe %s): %s",
                                turn, current_step_index, probe_spec.name, payload)
                    return self._build_result(
                        "fail",
                        f"step {current_step_index} fail (probe {probe_spec.name}): {payload}",
                        turn, start_time, steps_detail, scenario_steps, step_status,
                    )

                # kind == "retry"：数据还没到，等一会儿重查（本轮不算无进展）
                turns_without_progress = max(0, turns_without_progress - 1)
                time.sleep(float(payload))
                continue

            # ── See — 截图（纯视觉，不取 UI 树）──
            # settle 闸（Phase 1）：先等屏幕加载完/稳定再取帧，避免在过渡/加载态上
            # 决策定坐标（点空）或判断（半成品屏假失败）。窗口帧留给下面 Then 采样用。
            log.info("[Turn %d/step %d] 截图...", turn, current_step_index)
            settle_window: list[bytes] = []
            try:
                if self.settle_enabled:
                    try:
                        _settled, settle_window = wait_settled(
                            self.platform, timeout_s=self.settle_timeout,
                            interval=self.settle_interval,
                            stable_needed=self.settle_stable_frames)
                    except Exception as se:
                        log.debug("[Turn %d] settle 异常，退回单帧: %s", turn, se)
                        settle_window = []
                raw_bytes = settle_window[-1] if settle_window else self.platform.screenshot_raw()
                screenshot_png = raw_bytes
                step_record["screenshot_png"] = screenshot_png
            except Exception as e:
                log.error("[Turn %d] 截图失败: %s", turn, e)
                step_record["error"] = f"screenshot failed: {e}"
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                # 截图持续失败也消耗 sub-action 配额，否则 platform 异常会让
                # 该 step 无限刷 turn 直到外层 max_steps 用完才退出
                # （实测见过 1.9s 跑完 20 turn 的 case，就是这种死循环）
                sub_actions_in_step += 1
                continue

            raw_image = Image.open(io.BytesIO(raw_bytes))
            screen_size = self.platform.screen_size
            scale = getattr(self.platform, 'scale', None)
            if not scale:
                # 平台未上报 scale 时按实际数据推导：截图像素宽 / 逻辑屏宽
                # （Android screencap 通常 1:1，不能像旧代码那样写死 2.0 假值）
                scale = raw_image.width / screen_size[0] if screen_size[0] else 1.0
            try:
                ime_visible = self.platform.is_ime_visible()
            except Exception:
                ime_visible = False

            ctx = SkillContext(
                raw_image=raw_image, image=raw_image,
                screen_size=screen_size, scale=scale,
                prev_image=prev_raw_image, history=self.brain.history,
                ime_visible=ime_visible,
            )
            ctx = run_pipeline(self.skills_pipeline, ctx)
            prev_raw_image = raw_image

            # ── 分层执行(split_act_check)：操作步(When/Given) = ①大模型拆序列 ②小模型批量
            #    执行 ③下一轮大模型验证。检查步(Then/But)直接走下方 brain 路径。开关平台通用
            #    (桌面 + 移动端同生效)。拆步为空 → 回退 brain 逐步;批量中途元素定位失败/验证
            #    未达成 → 交下方 brain 看屏纠错(dismiss 弹窗 + 重拆，阶段3)。──
            idx = current_step_index
            is_action_step = (self.split_act_check
                              and 1 <= idx <= len(assertion_flags)
                              and not assertion_flags[idx - 1])
            if is_action_step and not batch_done and act_fails < ESCAPE_ACTION_FAILS:
                step_text = scenario_steps[idx - 1]
                evidence_ctx = "；".join(e for e in completed_evidence if e)  # 勿用 ctx(会盖 SkillContext)
                seq = self.brain.plan_step_actions(raw_bytes, step_text, evidence_ctx)  # ① 大模型拆步
                if seq:
                    ok, done_n, note = self._run_action_sequence(seq, screen_size)  # ② 小模型批量执行
                    after_bytes = raw_bytes
                    try:
                        after_bytes = self.platform.screenshot_raw()
                        prev_raw_image = Image.open(io.BytesIO(after_bytes))
                    except Exception:
                        pass
                    self.brain.note_external_action(
                        f"[分层批量执行] {note}", {"type": "batch", "n": len(seq)}, after_bytes)
                    step_record.update(action={"type": "batch", "n": len(seq)},
                                       observation=f"[分层批量] {note}",
                                       duration=time.time() - turn_start)
                    steps_detail.append(step_record)
                    if ok:
                        batch_done = True   # 全部执行成功 → 下一轮 fall through 到 brain 验证(③)
                        log.info("[Turn %d] 操作步 %d 批量执行 %d/%d 完成 → 下一轮 brain 验证",
                                 turn, idx, done_n, len(seq))
                    else:
                        # ③ 纠错闭环:批量中途 元素定位失败(疑似权限弹窗/遮挡挡住目标) →
                        # **不推进、下一轮重拆序列**;plan_step_actions 看屏会把"点掉弹窗"纳入新序列
                        # 开头(见其 prompt)，从而先 dismiss 再继续。连续失败达 ESCAPE_ACTION_FAILS
                        # → 逃生回 brain 逐步(brain 亲自看屏处理)。
                        act_fails += 1
                        log.warning("[Turn %d] 操作步 %d 批量中断于第 %d/%d(%s)→ 下一轮%s",
                                    turn, idx, done_n, len(seq), note,
                                    "逃生回 brain 逐步" if act_fails >= ESCAPE_ACTION_FAILS
                                    else "重拆序列(含弹窗处理)")
                    time.sleep(self.step_delay)
                    continue
                log.info("[Turn %d] 操作步 %d 拆步为空，回退 brain 逐步决策", turn, idx)

            # ── 连续断言合并（Phase 3，gated AGENT_MERGE_ASSERTS）：当前是断言步且后面还有
            #    连续断言步（同屏、中间无操作步）→ 一次调用逐条判 + 逐条硬墙 + 去重 + 负向加压，
            #    一次推进多步。合并失败/被拒回退逐步验证。──
            if (self.merge_asserts
                    and 1 <= current_step_index <= len(assertion_flags)
                    and assertion_flags[current_step_index - 1]):
                _blk = _consecutive_assert_block(assertion_flags, current_step_index)
                # 挂了 probe 的断言步不能进合并块 —— 合并会一次推进多步，等于让 brain
                # 视觉判掉本该走插件的非视觉断言（要么假 PASS，要么必 fail）。截到它前一步。
                if _blk and probes_by_step:
                    _cut = min((k for k in probes_by_step if _blk[0] <= k <= _blk[1]),
                               default=None)
                    if _cut is not None:
                        _blk = (_blk[0], _cut - 1)
                if _blk and _blk[1] > _blk[0]:           # 至少 2 条才合并
                    _i, _j = _blk
                    if self.settle_enabled and settle_window:
                        _frames, _dyn = sample_then_frames(settle_window)
                    else:
                        _frames = [raw_bytes]
                    _ev_ctx = "；".join(e for e in completed_evidence if e)
                    kind, payload = self._verify_assert_block(
                        _i, _j, _frames, _ev_ctx, retry_feedback, scenario_steps,
                        completed_evidence, step_status, steps_detail, screenshot_png,
                        turn, start_time, n_steps, screen_size)
                    if kind == "reject":
                        rejects_in_step += 1
                        log.warning("[Turn %d] 合并断言校验 REJECT (#%d/%d): %s",
                                    turn, rejects_in_step, MAX_REJECTS_PER_STEP, payload)
                        if rejects_in_step >= MAX_REJECTS_PER_STEP:
                            step_status[current_step_index] = "fail"
                            for m in range(current_step_index + 1, n_steps + 1):
                                step_status[m] = "skip"
                            return self._build_result(
                                "fail",
                                f"step {current_step_index} 合并断言连续 {MAX_REJECTS_PER_STEP} "
                                f"次校验被拒：{payload}",
                                turn, start_time, steps_detail, scenario_steps, step_status)
                        retry_feedback = payload
                        time.sleep(self.step_delay)
                        continue
                    if kind == "result":
                        return payload
                    if kind == "advance":
                        current_step_index = _j + 1
                        sub_actions_in_step = 0
                        rejects_in_step = 0
                        turns_without_progress = 0
                        wait_elapsed_in_step = 0.0
                        locate_attempts_in_step = 0
                        act_fails = 0
                        batch_done = False
                        retry_feedback = ""
                        time.sleep(self.step_delay)
                        continue
                    # kind == "fallback" → 落到下方逐步验证路径（不 continue）
                    log.info("[Turn %d] 合并断言 LLM/解析失败，回退逐步验证", turn)

            # ⚠️ no_effect 判断已**停用** —— 它靠 visual-diff 像素变化判"上次点击没生效"，但对
            # 小变化 UI(计算器数字、输入单字符、toggle/勾选)会**误判**成没生效(实测计算器
            # change 0.42% 被判 no_effect)，进而误触发网格图 / 元素定位重点 / 算无进展，
            # 又慢又不稳(拆步 11×15 因此 fail)。现在:坐标靠"每 tap 元素定位前置"保证准，
            # 点没点上交给 brain 看下一张截图自己判断。原逻辑注释保留于下，便于日后恢复/调参。
            #
            # ── 原 no_effect 判断(停用，注释保留备查)──
            # if turn > 1 and self.brain.history:
            #     prev_entry = self.brain.history[-1]
            #     prev_action_type = (prev_entry.get("action") or {}).get("type", "")
            #     if prev_action_type in ("tap", "swipe", "swipe_up", "swipe_down",
            #                             "scroll_up", "scroll_down", "press_key"):
            #         diff_res = ctx.skill_results.get("visual_diff")
            #         if diff_res and not diff_res.metadata.get("changed", True):
            #             if prev_action_type == "tap" and ime_visible and not prev_ime_visible:
            #                 prev_entry["focused_input"] = True
            #                 consec_no_effect = 0
            #             else:
            #                 prev_entry["no_effect"] = True
            #                 consec_no_effect += 1
            #         else:
            #             consec_no_effect = 0
            # ── 原逻辑结束 ──
            #
            # 现仅保留"tap 唤起软键盘 = 聚焦成功"这个正向提示(靠 is_ime_visible，不依赖 visual-diff):
            if (turn > 1 and self.brain.history
                    and (self.brain.history[-1].get("action") or {}).get("type") == "tap"
                    and ime_visible and not prev_ime_visible):
                self.brain.history[-1]["focused_input"] = True
                log.info("[Turn %d] 上次 tap 唤起软键盘，判定输入框已聚焦", turn)

            prev_ime_visible = ime_visible  # 记录本回合键盘态，供下一回合检测 tap 是否唤起键盘

            # ── 元素定位兜底：连续点空 → 代码层换专用定位模型重定位并直接重 tap ──
            # 不再把控制权交回 brain 让它盲猜同一坐标（见记忆 no-blind-retry）。
            # 仅在 元素定位启用 + 达阈值 + 未超本 step 上限 + 上次是带 target 的 tap 时触发。
            if (self.locator.enabled
                    and consec_no_effect >= self.locate_retry
                    and locate_attempts_in_step < MAX_LOCATE_PER_STEP
                    and self.brain.history):
                last_action = (self.brain.history[-1].get("action") or {})
                target_desc = (last_action.get("target")
                               if last_action.get("type") in ("tap", "long_press") else None)
                if target_desc:
                    coord = self.locator.locate(raw_bytes, target_desc, screen_size)
                    if coord is not None:
                        gx, gy = coord
                        locate_attempts_in_step += 1
                        ground_action = {"type": last_action.get("type", "tap"),
                                         "x": gx, "y": gy, "target": target_desc}
                        if last_action.get("type") == "long_press":
                            ground_action["duration"] = last_action.get("duration", 1.5)
                        log.info("[Turn %d] 元素定位重定位「%s」→ (%d,%d)，代码层重点击",
                                 turn, target_desc, gx, gy)
                        try:
                            self.platform.execute_action(ground_action)
                        except Exception as e:
                            log.warning("[Turn %d] 元素定位重点击执行失败: %s", turn, e)
                        # 记为一次外部动作（保持 brain history 不变式 + 让下轮 LLM 知道已重定位）
                        self.brain.note_external_action(
                            f"[元素定位] 按目标「{target_desc}」重定位并重新点击 ({gx},{gy})",
                            ground_action, screenshot_png)
                        consec_no_effect = 0  # 给这次 定位点击一个干净的效果判定窗口
                        step_record.update(
                            action=ground_action,
                            observation=f"[元素定位] 重定位重点击「{target_desc}」",
                            duration=time.time() - turn_start,
                        )
                        steps_detail.append(step_record)
                        time.sleep(self.step_delay)
                        continue

            # 定位不准的重试阶梯（按连续 no_effect 次数升级）：
            #   首次(0)      —— brain 原始坐标
            #   元素定位启用时先由上面的 元素定位兜底重定位（≥locate_retry 次）
            #   重试 ≥3 次   —— 发坐标网格红线图，让 LLM 照网格读精确坐标（保持到命中为止）
            use_grid = consec_no_effect >= 3
            if use_grid:
                log.info("[Turn %d] 连续 %d 次 no_effect，发坐标网格图帮 LLM 读精确坐标",
                         turn, consec_no_effect)

            # ── 断言型 step 帧采样（#4 / Phase 1）：网格兜底轮不采（那轮在纠偏定位）。──
            #   settle 开：从 settle 窗口采「首/中/稳」+ 2% 路由——静态断言只送稳态 1 帧，
            #     有过程/动画/瞬态才送 3 帧让 brain 判过程（横跨窗口，不漏早期就消失的 toast）。
            #   settle 关：退回旧「无条件多帧」路径。
            burst_frames: list[bytes] = []
            is_assert = (1 <= current_step_index <= len(assertion_flags)
                         and assertion_flags[current_step_index - 1])
            if is_assert and not use_grid:
                if self.settle_enabled and settle_window:
                    chosen, dynamic = sample_then_frames(settle_window)
                    if chosen:
                        if dynamic and len(chosen) > 1:
                            burst_frames = chosen           # 动态 → 首/中/稳 3 帧
                        screenshot_png = chosen[-1]         # 稳态帧为权威当前截图
                        step_record["screenshot_png"] = screenshot_png
                        log.info("[Turn %d] 断言采样: %s (%d 帧)",
                                 turn, "动态" if dynamic else "静态", len(chosen))
                elif self.assert_burst_frames > 1:
                    try:
                        burst_frames = self.platform.screenshot_burst(
                            count=self.assert_burst_frames, interval=0.25)
                        if burst_frames:
                            screenshot_png = burst_frames[-1]
                            step_record["screenshot_png"] = screenshot_png
                    except Exception as e:
                        log.debug("[Turn %d] 断言多帧抓取失败，退回单帧: %s", turn, e)
                        burst_frames = []

            # ── 3. Think — LLM 决策（带 step 上下文 + planner hint + retry_feedback）──
            t_llm = time.time()
            decision = self.brain.decide(
                test_case, screenshot_png, screen_size,
                skill_context=ctx,
                scenario_steps=scenario_steps,
                current_step_index=current_step_index,
                completed_evidence=completed_evidence,
                plan_hint=plan_hints_by_idx.get(current_step_index, ""),
                retry_feedback=retry_feedback,
                use_grid=use_grid,
                burst_frames=burst_frames,
                reference_images=reference_images,
            )
            log.info("[Turn %d] LLM 决策完成 (%.2fs)", turn, time.time() - t_llm)

            if decision is None:
                log.error("[Turn %d] LLM 决策失败", turn)
                step_record["error"] = "LLM decision failed"
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                # LLM 持续失败也应消耗当前 step 的 sub-action 配额，否则
                # PER_STEP_SUB_ACTION_LIMIT 起不到保护作用，会熬到 max_steps
                # 才退出（如对抗测试时撞到 400 Auth 错误连刷 40 turn 的情况）
                sub_actions_in_step += 1
                continue

            # ── 4. Validate step_progress ──
            ok, reject_reason = validate_step_progress(
                decision, prev_index=current_step_index, total_steps=n_steps,
            )
            step_record.update(
                observation=decision.get("observation", ""),
                thinking=decision.get("thinking", ""),
                action=decision.get("action"),
                step_progress=decision.get("step_progress"),
            )

            if not ok:
                rejects_in_step += 1
                log.warning("[Turn %d] step_progress REJECTED (#%d/%d 同 step 内): %s",
                            turn, rejects_in_step, MAX_REJECTS_PER_STEP, reject_reason)
                step_record["rejected"] = True
                step_record["reject_reason"] = reject_reason
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                retry_feedback = reject_reason
                # 被拒的决策从未执行 —— 从 brain history 里撤掉，否则下一轮
                # prompt 的「历史操作」会把它当已执行动作，且 no_effect 检测
                # 会把这个没执行的动作误标 no_effect 升级吸附/网格阶梯
                self.brain.discard_last()
                # 不执行 action，不消耗 sub-action 配额，直接下一轮让 LLM 修正
                continue

            # 通过校验，清空 retry_feedback
            retry_feedback = ""
            rejects_in_step = 0
            sub_actions_in_step += 1

            sp = decision["step_progress"]
            status = sp["current_step_status"]
            llm_step_idx = sp["current_step_index"]
            action = decision.get("action") or {}
            action_type = action.get("type", "")

            log.info("[Turn %d] step_progress: idx=%d status=%s action=%s",
                     turn, llm_step_idx, status, action_type)

            # ── 5. State 推进 ──
            if status == "pass":
                # 当前 step 通过：记录 evidence，advance
                ev = sp.get("evidence", "")
                while len(completed_evidence) < llm_step_idx:
                    completed_evidence.append("")
                completed_evidence[llm_step_idx - 1] = ev
                step_status[llm_step_idx] = "pass"
                log.info("[Turn %d] ✅ Step %d PASS: %s", turn, llm_step_idx, ev)

                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)

                if llm_step_idx >= n_steps:
                    # 最后一个 step 也过了 — 整 case 成功
                    return self._build_result(
                        "pass", "all steps passed",
                        turn, start_time, steps_detail, scenario_steps, step_status,
                    )
                # 推进到下一 step，重置 per-step 计数
                current_step_index = llm_step_idx + 1
                sub_actions_in_step = 0
                rejects_in_step = 0
                turns_without_progress = 0   # 推进了 → 清零无进展计数
                wait_elapsed_in_step = 0.0   # 新 step 重置等待预算
                locate_attempts_in_step = 0  # 新 step 重置 元素定位兜底次数
                act_fails = 0  # 新 step 重置分层操作失败计数
                batch_done = False  # 新 step 重置批量执行标记
                probe_attempts_in_step = 0   # 新 step 重置 probe 查询次数
                step_started_at = time.time()  # probe 的等数据预算从新 step 起算
                time.sleep(self.step_delay)
                continue

            if status == "fail":
                fail_reason = sp.get("fail_reason", "")
                step_status[llm_step_idx] = "fail"
                for i in range(llm_step_idx + 1, n_steps + 1):
                    step_status[i] = "skip"
                log.warning("[Turn %d] ❌ Step %d FAIL: %s", turn, llm_step_idx, fail_reason)
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                return self._build_result(
                    "fail", f"step {llm_step_idx} fail: {fail_reason}",
                    turn, start_time, steps_detail, scenario_steps, step_status,
                )

            # status == "in_progress" — 执行 action 推进当前 step

            # ── wait 动作（Phase 2：wait_for / 与 no-progress 解耦）──
            #   brain 主动等待（等加载 / 等结果出现）时：本轮**不计入** turns_without_progress
            #   （撤回 line 内先前 +1），改由 per-step 墙钟预算 self.wait_max_s 兜底——避免慢加载
            #   >MAX_TURNS_WITHOUT_PROGRESS 轮被误判假失败。等待本身：settle 开时用代码层
            #   wait_settled 等到屏幕稳定（零 LLM 的"等加载完"）；再兜一个下限短睡，让"已稳定但
            #   结果未出现"的异步态也推进墙钟。预算耗尽 → 恢复计数、落常规路径最终收敛。
            if action_type == "wait" and wait_elapsed_in_step < self.wait_max_s:
                turns_without_progress = max(0, turns_without_progress - 1)  # 撤回本轮无进展计数
                remaining = self.wait_max_s - wait_elapsed_in_step
                t_wait = time.time()
                if self.settle_enabled:
                    try:
                        wait_settled(self.platform,
                                     timeout_s=min(self.settle_timeout, remaining),
                                     interval=self.settle_interval,
                                     stable_needed=self.settle_stable_frames)
                    except Exception as we:
                        log.debug("[Turn %d] wait settle 异常: %s", turn, we)
                floor = min(2.0, max(0.0, remaining - (time.time() - t_wait)))
                if floor > 0:
                    time.sleep(floor)
                wait_elapsed_in_step += time.time() - t_wait
                log.info("[Turn %d] 等待中: 累计 %.1fs / 预算 %.0fs（不计无进展）",
                         turn, wait_elapsed_in_step, self.wait_max_s)
                step_record["observation"] = (
                    f"[等待] 累计 {wait_elapsed_in_step:.1f}s / {self.wait_max_s:.0f}s")
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                continue
            if action_type == "wait":
                log.warning("[Turn %d] 等待预算 %.0fs 耗尽仍未推进，恢复 no-progress 计数",
                            turn, self.wait_max_s)

            # ── brain 判断 + 元素定位:每个 tap/long_press 一律用 元素定位按 target
            #    定位坐标（大模型只负责判断"点哪个元素"，坐标交定位小模型 —— 实测大模型
            #    估坐标偏 ~7% 是点空主因）。元素定位未启用/无 target/没定位到 → 回退 brain 坐标。
            if (self.locator.enabled and isinstance(action, dict)
                    and action.get("type") in ("tap", "long_press") and action.get("target")):
                _coord = self.locator.locate(raw_bytes, action["target"], screen_size)
                if _coord is not None:
                    action["x"], action["y"] = _coord
                    log.info("[Turn %d] 元素定位「%s」→ (%d,%d)（替换 brain 坐标）",
                             turn, action["target"], _coord[0], _coord[1])
            log.info("[Turn %d] 执行动作: %s", turn, action)
            last_err = None
            for attempt in range(1, ACTION_MAX_RETRIES + 1):
                try:
                    self.platform.execute_action(action)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    log.warning("[Turn %d] 执行失败 (第%d次): %s", turn, attempt, e)
                    if attempt < ACTION_MAX_RETRIES:
                        time.sleep(0.5)

            if last_err:
                log.error("[Turn %d] 执行最终失败: %s", turn, last_err)
                step_record["error"] = str(last_err)

            step_record["duration"] = time.time() - turn_start
            steps_detail.append(step_record)
            time.sleep(self.step_delay)

        # while True 仅通过上面的各 return 退出（推进完成 / 各上限 fail）。
        # 防御性兜底：理论不可达，防未来误加 break 时静默漏判。
        log.warning("主循环异常退出（不应到达）")
        for i in step_status:
            if step_status[i] == "pending":
                step_status[i] = "skip"
        return self._build_result(
            "timeout", "loop exited unexpectedly",
            turn, start_time, steps_detail, scenario_steps, step_status,
        )

    def _verify_assert_block(self, i: int, j: int, frames: list[bytes], ev_ctx: str,
                             retry_feedback: str, scenario_steps: list[str],
                             completed_evidence: list[str], step_status: dict,
                             steps_detail: list, screenshot_png: bytes,
                             turn: int, start_time: float, n_steps: int,
                             screen_size: tuple[int, int]):
        """批量验证连续断言块 [i..j]（Phase 3）。返回 (kind, payload)：

          ('fallback', None)      —— LLM/解析失败，调用方回退逐步验证
          ('reject', reason)      —— 校验未过，调用方计 reject 并重试
          ('result', result_dict) —— 真 fail（关弹窗后仍 fail / 无弹窗）或已验到末步 → 调用方 return
          ('advance', j)          —— 全过且非末步，调用方 current_step_index=j+1 继续

        全 pass 才一次推进多步；**有 fail 先探测弹窗**：合并是同步 inline、设备还在那屏，若有
        拦截性弹窗挡住断言目标 → **直接关掉、重截图、重判**（最多 MAX_POPUP_DISMISS 次，防叠层
        弹窗/死循环），避免"弹窗假 fail"；关掉后仍 fail、或压根没弹窗 → 真 fail。逐条硬墙 + 去重
        + 负向加压由 validate_assertion_batch 兜底。
        """
        assertions = [{"index": k, "text": scenario_steps[k - 1]} for k in range(i, j + 1)]

        def _record_pass(k: int, ev: str) -> None:
            while len(completed_evidence) < k:
                completed_evidence.append("")
            completed_evidence[k - 1] = ev
            step_status[k] = "pass"
            steps_detail.append({
                "screenshot_png": screenshot_png, "step_index": k,
                "observation": f"[合并断言] {ev}",
                "step_progress": {"current_step_index": k, "current_step_status": "pass",
                                  "evidence": ev, "fail_reason": ""},
                "duration": time.time() - start_time,
            })
            log.info("[Turn %d] ✅(合并) Step %d PASS: %s", turn, k, ev)

        by_id: dict[int, dict] = {}
        dismiss_n = 0
        while True:
            results = self.brain.verify_assertions_batch(frames, assertions, ev_ctx, retry_feedback)
            if results is None:
                return ("fallback", None)
            ok, reason = validate_assertion_batch(results, assertions)
            if not ok:
                return ("reject", reason)
            by_id = {}
            for r in results:
                try:
                    by_id[int(r["id"])] = r
                except (TypeError, ValueError, KeyError):
                    pass
            first_fail = next((k for k in range(i, j + 1)
                               if by_id.get(k, {}).get("verdict") != "pass"), None)
            if first_fail is None:
                break                                   # 全 pass

            # 有 fail → 先探测是不是拦截性弹窗挡住的
            if dismiss_n < MAX_POPUP_DISMISS:
                act = self.brain.dismiss_blocking_popup(screenshot_png)
                if act:
                    dismiss_n += 1
                    if self.locator.enabled and act.get("target"):
                        c = self.locator.locate(screenshot_png, act["target"], screen_size)
                        if c:
                            act["x"], act["y"] = c
                    if "x" not in act and act.get("x_pct") is not None:
                        w, h = screen_size
                        act["x"] = int(round(float(act["x_pct"]) / 100.0 * w))
                        act["y"] = int(round(float(act["y_pct"]) / 100.0 * h))
                    log.info("[Turn %d] 合并 fail@step %d 疑似弹窗，关闭「%s」后重判",
                             turn, first_fail, act.get("target", ""))
                    try:
                        self.platform.execute_action(act)
                    except Exception as e:
                        log.warning("[Turn %d] 关弹窗执行失败: %s", turn, e)
                    self.brain.note_external_action(
                        f"[合并断言] 关闭拦截弹窗「{act.get('target', '')}」后重判", act, screenshot_png)
                    time.sleep(self.step_delay)
                    # 重截 + 重采样（关掉弹窗后屏幕变了）
                    window: list[bytes] = []
                    if self.settle_enabled:
                        try:
                            _s, window = wait_settled(
                                self.platform, timeout_s=self.settle_timeout,
                                interval=self.settle_interval,
                                stable_needed=self.settle_stable_frames)
                        except Exception:
                            window = []
                    screenshot_png = window[-1] if window else self.platform.screenshot_raw()
                    frames = sample_then_frames(window)[0] if window else [screenshot_png]
                    retry_feedback = ""
                    continue                            # 重判

            # 没弹窗 / 关到上限仍 fail → 真 fail
            for k in range(i, first_fail):
                _record_pass(k, by_id.get(k, {}).get("evidence", ""))
            r = by_id.get(first_fail, {})
            fr = r.get("fail_reason", "")
            step_status[first_fail] = "fail"
            for m in range(first_fail + 1, n_steps + 1):
                step_status[m] = "skip"
            steps_detail.append({
                "screenshot_png": screenshot_png, "step_index": first_fail,
                "observation": f"[合并断言] {r.get('evidence', '') or fr}",
                "step_progress": {"current_step_index": first_fail, "current_step_status": "fail",
                                  "evidence": r.get("evidence", ""), "fail_reason": fr},
                "duration": time.time() - start_time,
            })
            log.warning("[Turn %d] ❌(合并) Step %d FAIL: %s", turn, first_fail, fr)
            return ("result", self._build_result(
                "fail", f"step {first_fail} fail(merged): {fr}",
                turn, start_time, steps_detail, scenario_steps, step_status))

        # 全 pass → 一次推进多步
        for k in range(i, j + 1):
            _record_pass(k, by_id.get(k, {}).get("evidence", ""))
        if j >= n_steps:
            return ("result", self._build_result(
                "pass", "all steps passed (merged asserts)",
                turn, start_time, steps_detail, scenario_steps, step_status))
        return ("advance", j)

    def _run_action_step(self, act: dict, before_bytes: bytes,
                         screen_size: tuple[int, int]) -> tuple[str, bytes, str]:
        """分层执行:按 planner 的结构化动作直接操作(不调大 LLM)。

        tap/input/long_press 用 元素定位按 target 定位坐标;swipe 类/按键/滚动直接调
        platform 原语。执行后回看一张截图,visual-diff 判"界面是否产生变化"作为生效信号。

        返回 (result, after_bytes, note):
          result ∈ 'advanced'(生效,推进) | 'no_effect'(执行了但无变化) |
                   'locate_fail'(元素定位没定位到) | 'exec_fail'(执行异常/不支持)
        """
        atype = act.get("type", "")
        target = act.get("target", "")
        label = target or act.get("key") or act.get("value") or ""
        note = f"{atype} {label}".strip()

        # 1. 需要视觉定位的动作:元素定位按 target 找坐标
        px = None
        if atype in ("tap", "input", "long_press"):
            if not self.locator.enabled:
                return ("locate_fail", before_bytes, f"{note}(元素定位未启用无法定位)")
            px = self.locator.locate(before_bytes, target, screen_size)
            if px is None:
                return ("locate_fail", before_bytes, f"{note}(元素定位未定位到「{target}」)")

        # 2. 执行动作
        try:
            if atype == "tap":
                self.platform.tap(*px)
            elif atype == "long_press":
                self.platform.execute_action(
                    {"type": "long_press", "x": px[0], "y": px[1],
                     "duration": float(act.get("duration", 1.5))})
            elif atype == "input":
                self.platform.tap(*px)
                time.sleep(0.5)
                self.platform.input_text(act.get("value", ""))
            elif atype == "scroll_up":
                self.platform.scroll_up()
            elif atype == "scroll_down":
                self.platform.scroll_down()
            elif atype == "press_key":
                self.platform.press_key(act.get("key", "enter"))
            elif atype == "back":
                self.platform.press_key("back")
            elif atype == "open_app":
                self.platform.open_target(target or act.get("value", ""))
            else:
                # swipe(需自定义坐标)等 MVP 不在分层内直接处理 → 逃生大 LLM
                return ("exec_fail", before_bytes, f"{note}(分层暂不支持 {atype},交大 LLM)")
        except Exception as e:
            return ("exec_fail", before_bytes, f"{note}(执行异常: {e})")

        # 执行成功即推进 —— **不做视觉自我验证**。用 before/after 像素变化判"生效"对小变化
        # UI(计算器数字、toggle、勾选等)会误判 no_effect，导致操作步全部逃生 + 重复点击
        # (实测 11×15 因此 44 turn/fail)。分层的正确语义:操作步只执行 + 推进,点得对不对
        # 交给后面的检查步(Then)兜底 —— 这才是 argus step 推进的本意。
        time.sleep(self.step_delay)
        try:
            after_bytes = self.platform.screenshot_raw()
        except Exception:
            after_bytes = before_bytes
        return ("advanced", after_bytes, note)

    def _run_action_sequence(self, seq: list[dict],
                             screen_size: tuple[int, int]) -> tuple[bool, int, str]:
        """【小模型·批量执行】把大模型拆出的原子动作序列逐个用 元素定位并执行。

        复用 _run_action_step(每个动作:元素定位按 target 定位 + 执行 + 回看截图)。中途某个
        动作 元素定位失败 / 执行异常 → 停止并返回 (False, 已执行数, 说明)，交上层 brain
        看屏纠错(dismiss 弹窗 + 重拆，阶段3)；全部成功 → (True, n, 说明)。
        """
        before = self.platform.screenshot_raw()
        for i, act in enumerate(seq):
            result, after, note = self._run_action_step(act, before, screen_size)
            if result != "advanced":
                return (False, i, f"第 {i + 1}/{len(seq)} 个动作失败: {note}")
            before = after
        return (True, len(seq), f"{len(seq)} 个动作全部执行完毕")

    # ─────────────────────────────────────────────────────────
    # 非视觉断言（probe 插件）
    # ─────────────────────────────────────────────────────────

    def _probe_ctx(self, scenario_steps: list[str], *, step_index: int, step_text: str,
                   case_started_at: float, step_started_at: float, attempt: int,
                   elapsed_s: float, timeout_s: float, case_text: str) -> ProbeContext:
        """组装喂给 probe 的上下文 —— 关键是让插件能把「本次跑测产生的数据」
        从历史数据里框出来：时间窗（case/step 起点）+ 身份锚点（账号/设备/包名）。"""
        from pathlib import Path

        pc = self.probe_context or {}
        cfg = self.cfg
        appium = cfg.get("appium") or {}
        package = (appium.get("package") or (cfg.get("android") or {}).get("package")
                   or appium.get("bundle_id") or "")
        device = (appium.get("device") or (cfg.get("android") or {}).get("serial") or "")

        m = _CASE_ID_RE.match(case_text.lstrip().splitlines()[0]) if case_text.strip() else None
        case_id = pc.get("case_id") or (m.group(1) if m else "")

        artifacts_dir = pc.get("artifacts_dir") or ""
        if not artifacts_dir:
            artifacts_dir = str(Path(".argus_runs") / "probe_artifacts"
                                / (pc.get("run_id") or "local") / (case_id or "case"))
        try:
            Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.debug("probe artifacts 目录创建失败（probe 可忽略该字段）: %s", e)

        return ProbeContext(
            step_index=step_index, step_text=step_text,
            scenario_steps=list(scenario_steps),
            case_started_at=case_started_at, step_started_at=step_started_at,
            now=time.time(), attempt=attempt, elapsed_s=elapsed_s, timeout_s=timeout_s,
            platform=getattr(self.platform, "platform_name", "") or cfg.get("platform", ""),
            device=str(device), app_package=str(package),
            app_version=str(getattr(self.platform, "app_version", "") or ""),
            run_id=str(pc.get("run_id") or ""), case_id=str(case_id),
            target=str(pc.get("target") or ""),
            account=dict(pc.get("account") or {}),
            artifacts_dir=artifacts_dir,
        )

    def _skip_probe_step(self, spec: ProbeSpec, step_index: int,
                         step_record: dict, turn: int) -> tuple[str, str]:
        """`--skip-probes`：不调插件，把该 step 标 skip 后推进。

        注意它**不是 pass** —— step_status 记 skip、reason 里点名哪几步没验证，
        报告上一眼能看出这份绿是有缺口的。用于数据通道挂了 / 只想过一遍 UI 回归。
        """
        note = f"[probe:{spec.name}] 按 --skip-probes 跳过，未验证（{spec.summary()}）"
        step_record.update(
            action={"type": "probe", "name": spec.name, "args": spec.args,
                    "skipped": True},
            observation=note,
            step_progress={"current_step_index": step_index,
                           "current_step_status": "skip", "evidence": note},
        )
        step_record["probe"] = {
            "name": spec.name, "spec": spec.summary(), "verdict": "skipped",
            "evidence": note, "error": "", "attempt": 0,
            "elapsed_s": 0.0, "timeout_s": 0.0, "data": "",
        }
        log.warning("[Turn %d] ⏭️ Step %d SKIP (probe %s，--skip-probes)",
                    turn, step_index, spec.name)
        return ("skip", note)

    def _run_probe_step(self, spec: ProbeSpec, step_index: int,
                        scenario_steps: list[str], case_text: str, *,
                        attempt: int, case_started_at: float, step_started_at: float,
                        step_record: dict, turn: int) -> tuple[str, object]:
        """跑一次非视觉断言（该 step 不进 LLM）。返回 (kind, payload)：

          ('pass', evidence)   —— 插件确认了，调用方推进到下一 step
          ('fail', reason)     —— 插件否定 / 预算耗尽仍拿不到数据 → 整 case fail
          ('retry', sleep_s)   —— 还查不到（可能只是上报还没 flush），等一会儿重查

        为什么 retry 而不是直接 fail：埋点这类链路普遍有批量上报延迟（分钟级），
        查太早的 0 行**不是**漏报证据。等到预算耗尽才下结论是框架保证的，不靠
        每个插件作者自觉。
        """
        timeout_s, poll_s = self.probes.limits(spec.name)
        elapsed = max(0.0, time.time() - step_started_at)
        step_text = (scenario_steps[step_index - 1]
                     if 1 <= step_index <= len(scenario_steps) else "")

        log.info("[Turn %d/step %d] probe「%s」第 %d 次查询（已等 %.0fs / 预算 %.0fs）",
                 turn, step_index, spec.summary(), attempt, elapsed, timeout_s)

        ctx = self._probe_ctx(
            scenario_steps, step_index=step_index, step_text=step_text,
            case_started_at=case_started_at, step_started_at=step_started_at,
            attempt=attempt, elapsed_s=elapsed, timeout_s=timeout_s, case_text=case_text)
        result = self.probes.check(spec, ctx)

        step_record["action"] = {"type": "probe", "name": spec.name, "args": spec.args}
        step_record["probe"] = {
            "name": spec.name, "spec": spec.summary(), "verdict": result.verdict,
            "evidence": result.evidence, "error": result.error, "attempt": attempt,
            "elapsed_s": round(elapsed, 1), "timeout_s": timeout_s,
            "data": summarize_data(result.data),
        }
        step_record["observation"] = (
            f"[probe:{spec.name}] {result.evidence or result.error or result.verdict}")

        terminal = result.is_pass or result.is_fail
        # 首次 + 终局各留一张截图给报告做现场对照；中间轮不留（否则 base64 撑爆 HTML）
        if attempt == 1 or terminal:
            try:
                step_record["screenshot_png"] = self.platform.screenshot_raw()
            except Exception as e:
                log.debug("probe step 截图失败（不影响判定）: %s", e)

        if result.is_pass:
            ev = f"[probe:{spec.name}] {result.evidence or '插件判定通过（未给 evidence）'}"
            step_record["step_progress"] = {
                "current_step_index": step_index, "current_step_status": "pass",
                "evidence": ev,
            }
            return ("pass", ev)

        if result.is_fail:
            reason = (result.evidence or result.error
                      or f"probe「{spec.summary()}」判定不通过（未给说明）")
            step_record["step_progress"] = {
                "current_step_index": step_index, "current_step_status": "fail",
                "evidence": f"[probe:{spec.name}] {reason}", "fail_reason": reason,
            }
            return ("fail", reason)

        # inconclusive —— 预算耗尽就落 fail，否则等一会儿重查
        if elapsed >= timeout_s or attempt >= MAX_ATTEMPTS_PER_STEP:
            reason = (f"probe「{spec.summary()}」等了 {elapsed:.0f}s / 查了 {attempt} 次"
                      f"仍拿不到可判定的数据（预算 {timeout_s:.0f}s）。"
                      f"最后一次: {result.evidence or result.error or '无说明'}")
            step_record["step_progress"] = {
                "current_step_index": step_index, "current_step_status": "fail",
                "evidence": f"[probe:{spec.name}] {reason}", "fail_reason": reason,
            }
            return ("fail", reason)

        sleep_s = poll_s if result.retry_after_s is None else float(result.retry_after_s)
        sleep_s = max(MIN_POLL_INTERVAL_S, sleep_s)
        sleep_s = min(sleep_s, max(MIN_POLL_INTERVAL_S, timeout_s - elapsed))
        step_record["step_progress"] = {
            "current_step_index": step_index, "current_step_status": "in_progress",
            "evidence": (f"[probe:{spec.name}] 暂未拿到可判定数据，{sleep_s:.0f}s 后重查"
                         f"（已等 {elapsed:.0f}s / 预算 {timeout_s:.0f}s）"),
        }
        log.info("[Turn %d/step %d] probe 未定论，%.0fs 后重查（不计无进展）",
                 turn, step_index, sleep_s)
        return ("retry", sleep_s)

    # 匹配 case body 里的参考图声明行：`- **Ref**: <path>[ | <path> ...]`
    _REF_LINE_RE = re.compile(r'^\s*-\s*\*\*Ref\*\*:\s*(.+?)\s*$')

    def _load_reference_images(self, case_text: str) -> list[tuple[str, bytes]]:
        """加载 case 声明的参考设计图（#5，供断言型 step 视觉走查对比）。

        gherkin.render_case 把 `@ref:<path>` / `# argus-ref:` 解析成绝对路径写进
        `- **Ref**:` 行（多张用 ` | ` 分隔）。这里读文件字节；缺失/读失败静默跳过
        （不阻塞跑测）。返回 [(文件名, png_bytes), ...]。
        """
        from pathlib import Path

        refs: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for line in case_text.splitlines():
            m = self._REF_LINE_RE.match(line)
            if not m:
                continue
            for raw in m.group(1).split("|"):
                path = raw.strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                try:
                    p = Path(path)
                    if p.is_file():
                        refs.append((p.name, p.read_bytes()))
                    else:
                        log.warning("参考图不存在，跳过: %s", path)
                except Exception as e:
                    log.warning("参考图读取失败 %s: %s", path, e)
        if refs:
            log.info("加载 %d 张参考图: %s", len(refs), [n for n, _ in refs])
        return refs

    def _build_result(self, result: str, reason: str, turns: int, start_time: float,
                      steps_detail: list, scenario_steps: list, step_status: dict) -> dict:
        # --skip-probes 跳过的非视觉断言必须写进 reason —— 否则一份全绿报告看不出
        # 某些断言其实压根没验证过（静默绿是这套设计最想避免的事）。
        skipped = getattr(self, "_probe_skipped_steps", None)
        if skipped:
            reason = (f"{reason}（注意：step {sorted(skipped)} 的非视觉断言按 "
                      f"--skip-probes 跳过，未验证）")
        return {
            "result": result,
            "reason": reason,
            "steps": turns,
            "duration": time.time() - start_time,
            "steps_detail": steps_detail,
            "scenario_steps": scenario_steps,
            "step_status": step_status,
            "probes_skipped_steps": sorted(skipped) if skipped else [],
        }
