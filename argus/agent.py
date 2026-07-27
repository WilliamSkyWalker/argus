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
from .grounding import GroundingLocator
from .logger import get_logger
from .planner import plan_scenario
from .platforms import create_platform
from .skills import SkillContext, create_pipeline, run_pipeline
from .step_validator import validate_step_progress

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

# 单个 step 内最多用 grounding 兜底重定位几次（超过则回落到网格兜底，防死循环）。
MAX_GROUNDING_PER_STEP = 2

# 断言型 step 关键字（触发多帧时间窗断言，见 #4）。And/But 的实际类别继承前一 primary。
_ASSERT_KEYWORDS = ("Then", "But", "那么", "但是")
_ACTION_KEYWORDS = ("When", "Given", "当", "假如", "如果", "前提")

# 匹配 Gherkin step 行（Given/When/Then/And/But + 后续文本）
_STEP_LINE_RE = re.compile(r'^\s*(Given|When|Then|And|But)\s+(.+)$')


def _extract_scenario_steps(case_text: str) -> list[str]:
    """从 case body 中提取 Scenario 的 step 列表（不含 Background）。

    匹配 argus.gherkin.render_case 输出格式：
        - **Steps**:
          Given xxx
          When xxx
          Then xxx
          And xxx
          But xxx
    """
    lines = case_text.splitlines()
    in_steps = False
    steps: list[str] = []
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
    return steps


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


class Agent:
    def __init__(self, config: dict | None = None):
        log.info("Agent.__init__ 开始")

        from .config import load_config
        cfg = config or load_config()
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
        # grounding 定位兜底（LLM_MODEL_GROUNDING 为空则 disabled）
        self.grounding = GroundingLocator(cfg["llm"].get("grounding"))
        if self.grounding.enabled:
            log.info("grounding 定位兜底已启用: model=%s", self.grounding.model)

        self.max_steps = cfg["agent"]["max_steps"]
        self.step_delay = cfg["agent"]["step_delay"]
        self.grounding_retry = int(cfg["agent"].get("grounding_retry", 2))
        self.assert_burst_frames = int(cfg["agent"].get("assert_burst_frames", 1))
        log.info("max_steps=%d, step_delay=%.1f", self.max_steps, self.step_delay)

        log.info("创建 skills pipeline...")
        self.skills_pipeline = create_pipeline(cfg.get("skills"))
        log.info("skills pipeline 创建完成: %s", [s.name for s in self.skills_pipeline])

        log.info("Agent.__init__ 完成")

    def run(self, test_case: str) -> dict:
        """Execute a test case with step-driven loop + validator gating."""
        self.brain.reset()
        log.info("=" * 60)
        log.info("测试用例: %s", test_case)
        log.info("=" * 60)

        # 提取 Scenario 的 step 列表（用于 step 级报告 + LLM narrative）
        scenario_steps = _extract_scenario_steps(test_case)
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
        grounding_attempts_in_step = 0  # 本 step 已用 grounding 兜底几次（cap 见 MAX_GROUNDING_PER_STEP）

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

            # ── See — 截图（纯视觉，不取 UI 树）──
            log.info("[Turn %d/step %d] 截图...", turn, current_step_index)
            try:
                raw_bytes = self.platform.screenshot_raw()
                screenshot_png = raw_bytes
                step_record["screenshot_png"] = screenshot_png
            except Exception as e:
                log.error("[Turn %d] 截图失败: %s", turn, e)
                step_record["error"] = f"screenshot failed: {e}"
                step_record["duration"] = time.time() - turn_start
                steps_detail.append(step_record)
                # 截图持续失败也消耗 sub-action 配额，否则 platform 异常会让
                # 该 step 无限刷 turn 直到外层 max_steps 用完才退出
                # （实测 07-engage 终局有 1.9s 跑完 20 turn 的 case 就是这种死循环）
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

            # no-effect 反馈：上一动作的像素变化 < 阈值 → 标记给 LLM 看
            if turn > 1 and self.brain.history:
                prev_entry = self.brain.history[-1]
                prev_action_type = (prev_entry.get("action") or {}).get("type", "")
                if prev_action_type in ("tap", "swipe", "swipe_up", "swipe_down",
                                        "scroll_up", "scroll_down", "press_key"):
                    diff_res = ctx.skill_results.get("visual_diff")
                    if diff_res and not diff_res.metadata.get("changed", True):
                        # 例外：tap 后软键盘从无→有 = 成功聚焦了输入框。键盘弹出在截图里
                        # 像素变化极小（visual_diff 会判 unchanged），但这是**有效**操作 —
                        # 不能当 no_effect 让 LLM 去重复点别处，要正向提示它「已聚焦，去 input」。
                        if prev_action_type == "tap" and ime_visible and not prev_ime_visible:
                            prev_entry["focused_input"] = True
                            consec_no_effect = 0  # 聚焦成功，重置卡住计数
                            log.info("[Turn %d] 上次 tap 唤起软键盘，判定输入框已聚焦"
                                     "（不计 no_effect，提示 LLM 直接 input）", turn)
                        else:
                            prev_entry["no_effect"] = True
                            consec_no_effect += 1
                            log.warning("[Turn %d] 上次 %s 无可见变化 (change=%.2f%%, 连续%d次)",
                                        turn, prev_action_type,
                                        diff_res.metadata.get("change_ratio", 0) * 100,
                                        consec_no_effect)
                    else:
                        consec_no_effect = 0  # 有可见变化，重置

            prev_ime_visible = ime_visible  # 记录本回合键盘态，供下一回合检测 tap 是否唤起键盘

            # ── grounding 兜底：连续点空 → 代码层换专用定位模型重定位并直接重 tap ──
            # 不再把控制权交回 brain 让它盲猜同一坐标（见记忆 no-blind-retry）。
            # 仅在 grounding 启用 + 达阈值 + 未超本 step 上限 + 上次是带 target 的 tap 时触发。
            if (self.grounding.enabled
                    and consec_no_effect >= self.grounding_retry
                    and grounding_attempts_in_step < MAX_GROUNDING_PER_STEP
                    and self.brain.history):
                last_action = (self.brain.history[-1].get("action") or {})
                target_desc = (last_action.get("target")
                               if last_action.get("type") in ("tap", "long_press") else None)
                if target_desc:
                    coord = self.grounding.locate(raw_bytes, target_desc, screen_size)
                    if coord is not None:
                        gx, gy = coord
                        grounding_attempts_in_step += 1
                        ground_action = {"type": last_action.get("type", "tap"),
                                         "x": gx, "y": gy, "target": target_desc}
                        if last_action.get("type") == "long_press":
                            ground_action["duration"] = last_action.get("duration", 1.5)
                        log.info("[Turn %d] grounding 重定位「%s」→ (%d,%d)，代码层重点击",
                                 turn, target_desc, gx, gy)
                        try:
                            self.platform.execute_action(ground_action)
                        except Exception as e:
                            log.warning("[Turn %d] grounding 重点击执行失败: %s", turn, e)
                        # 记为一次外部动作（保持 brain history 不变式 + 让下轮 LLM 知道已重定位）
                        self.brain.note_external_action(
                            f"[grounding] 按目标「{target_desc}」重定位并重新点击 ({gx},{gy})",
                            ground_action, screenshot_png)
                        consec_no_effect = 0  # 给这次 grounding 点击一个干净的效果判定窗口
                        step_record.update(
                            action=ground_action,
                            observation=f"[grounding] 重定位重点击「{target_desc}」",
                            duration=time.time() - turn_start,
                        )
                        steps_detail.append(step_record)
                        time.sleep(self.step_delay)
                        continue

            # 定位不准的重试阶梯（按连续 no_effect 次数升级）：
            #   首次(0)      —— brain 原始坐标
            #   grounding 启用时先由上面的 grounding 兜底重定位（≥grounding_retry 次）
            #   重试 ≥3 次   —— 发坐标网格红线图，让 LLM 照网格读精确坐标（保持到命中为止）
            use_grid = consec_no_effect >= 3
            if use_grid:
                log.info("[Turn %d] 连续 %d 次 no_effect，发坐标网格图帮 LLM 读精确坐标",
                         turn, consec_no_effect)

            # ── 多帧时间窗断言（#4）：断言型 step 抓一小段连续帧，让 toast/banner 等
            #    「出现过又消失」的瞬态 UI 可被判断。网格兜底轮不抓（那轮在纠偏定位）。──
            burst_frames: list[bytes] = []
            is_assert = (1 <= current_step_index <= len(assertion_flags)
                         and assertion_flags[current_step_index - 1])
            if is_assert and self.assert_burst_frames > 1 and not use_grid:
                try:
                    burst_frames = self.platform.screenshot_burst(
                        count=self.assert_burst_frames, interval=0.25)
                    if burst_frames:
                        # 末帧即最新态，作为本轮权威当前截图
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
                grounding_attempts_in_step = 0  # 新 step 重置 grounding 兜底次数
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

    @staticmethod
    def _build_result(result: str, reason: str, turns: int, start_time: float,
                      steps_detail: list, scenario_steps: list, step_status: dict) -> dict:
        return {
            "result": result,
            "reason": reason,
            "steps": turns,
            "duration": time.time() - start_time,
            "steps_detail": steps_detail,
            "scenario_steps": scenario_steps,
            "step_status": step_status,
        }
