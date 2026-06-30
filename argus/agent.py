"""Main agent loop — ties eyes, brain, and hands together via Platform.

Step-driven model (修正版档 3 — 「看全局，禁跳跃」):

  Outer loop iterates over Scenario steps. For each step we run an inner
  loop of LLM sub-actions (capped at ``PER_STEP_SUB_ACTION_LIMIT``) until
  the LLM declares ``current_step_status = pass``. On ``fail`` we abort
  the whole Scenario (Cucumber semantics).

  The LLM always sees the **full** step list (for narrative context) but
  every decision passes through ``step_validator``:
    - current_step_index must be monotonic (+0 or +1, never jump)
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
from .logger import get_logger
from .planner import plan_scenario
from .platforms import create_platform
from .dialog_dismisser import dismiss_known_dialogs
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

        self.max_steps = cfg["agent"]["max_steps"]
        self.step_delay = cfg["agent"]["step_delay"]
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
        n_steps = len(scenario_steps)
        step_status: dict[int, str] = {i: "pending" for i in range(1, n_steps + 1)}
        log.info("Scenario steps 提取: %d 步", n_steps)

        # ── Planner: 跑一次 LLM 把 case 拆成执行剧本，作为 hint 注入 brain ──
        # graceful: plan 失败为空，不阻塞 executor。
        plan_hints_by_idx: dict[int, str] = {}
        if n_steps > 0:
            try:
                t_plan = time.time()
                plan = plan_scenario(test_case, self.brain.client, self.brain.model,
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

        # turn = LLM 调用次数（含被 reject 的、含成功的 sub-action）。
        # max_steps 是兜底防失控；正常路径走 PER_STEP_SUB_ACTION_LIMIT 控流。
        for turn in range(1, self.max_steps + 1):
            turn_start = time.time()
            step_record = {
                "turn": turn,
                "gherkin_step_index": current_step_index,
                "screenshot_png": None, "action": None,
                "observation": "", "thinking": "", "error": None,
                "step_progress": None,
                "rejected": False, "reject_reason": "",
            }

            # ── 0. Per-step sub-action 上限保护（PER_STEP_SUB_ACTION_LIMIT < 0 时禁用，仅由 max_steps 兜底）──
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

            # ── 1. Pre-turn — dismiss 已知系统弹窗 ──
            try:
                dismissed = dismiss_known_dialogs(self.platform, test_case)
                if dismissed:
                    log.info("[Turn %d] 自动 dismiss: %s", turn, dismissed)
                    time.sleep(0.5)
            except Exception as e:
                log.debug("[Turn %d] dialog dismisser error (ignored): %s", turn, e)

            # ── 2. See — 截图 + UI tree ──
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

            ui_tree = self.platform.get_ui_tree()

            # Race-guard: 延迟弹出的 dialog
            try:
                late_dismiss = dismiss_known_dialogs(self.platform, test_case)
                if late_dismiss:
                    log.info("[Turn %d] 后置 dismiss: %s", turn, late_dismiss)
                    time.sleep(0.5)
                    raw_bytes = self.platform.screenshot_raw()
                    screenshot_png = raw_bytes
                    step_record["screenshot_png"] = screenshot_png
                    ui_tree = self.platform.get_ui_tree()
            except Exception as e:
                log.debug("[Turn %d] 后置 dismiss error (ignored): %s", turn, e)

            raw_image = Image.open(io.BytesIO(raw_bytes))
            screen_size = self.platform.screen_size
            scale = getattr(self.platform, 'scale', 2.0)
            try:
                ime_visible = self.platform.is_ime_visible()
            except Exception:
                ime_visible = False

            ctx = SkillContext(
                raw_image=raw_image, image=raw_image,
                ui_tree=ui_tree, screen_size=screen_size, scale=scale,
                prev_image=prev_raw_image, history=self.brain.history,
                ime_visible=ime_visible,
            )
            ctx = run_pipeline(self.skills_pipeline, ctx)
            prev_raw_image = raw_image

            # no-effect 反馈：上一动作的像素变化 < 阈值 → 标记给 LLM 看
            if turn > 1 and self.brain.history:
                prev_entry = self.brain.history[-1]
                prev_action_type = prev_entry.get("action", {}).get("type", "")
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

            # 定位不准的重试阶梯（按连续 no_effect 次数升级）：
            #   首次(0)      —— brain 原始坐标，不吸附不网格
            #   重试 1-2 次  —— 打开 tap 吸附，把坐标回吸到最近可交互节点（含输入框）
            #   重试 3-4 次  —— 发坐标网格红线图，让 LLM 照网格读精确坐标
            if hasattr(self.platform, "set_snap_enabled"):
                self.platform.set_snap_enabled(consec_no_effect in (1, 2))
            use_grid = consec_no_effect in (3, 4)
            if use_grid:
                log.info("[Turn %d] 连续 %d 次 no_effect，发坐标网格图帮 LLM 读精确坐标",
                         turn, consec_no_effect)

            # ── 3. Think — LLM 决策（带 step 上下文 + planner hint + retry_feedback）──
            t_llm = time.time()
            decision = self.brain.decide(
                test_case, screenshot_png, ui_tree, screen_size,
                skill_context=ctx,
                scenario_steps=scenario_steps,
                current_step_index=current_step_index,
                completed_evidence=completed_evidence,
                plan_hint=plan_hints_by_idx.get(current_step_index, ""),
                retry_feedback=retry_feedback,
                use_grid=use_grid,
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

        # 外层 max_steps 兜底（不应该走到这里 — per-step limit 应先触发）
        log.warning("达到外层 max_steps 限制 (%d)，测试未完成", self.max_steps)
        for i in step_status:
            if step_status[i] == "pending":
                step_status[i] = "skip"
        return self._build_result(
            "timeout", f"max_steps {self.max_steps} reached (per-step limit 也未触发，可能是 bug)",
            self.max_steps, start_time, steps_detail, scenario_steps, step_status,
        )

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
