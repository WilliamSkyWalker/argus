"""LLM Agent brain — reads screen, decides actions, judges results."""

import base64
import json
import re
import time

from openai import OpenAI

from .grid import img_to_png_bytes, draw_coordinate_grid
from .logger import get_logger

log = get_logger("brain")

LLM_MAX_RETRIES = 2
LLM_TIMEOUT_S = 30  # 单次 chat.completions.create 硬超时；OpenAI SDK 默认 600s 太长

# How many recent screenshots (besides current) to include for visual context.
HISTORY_IMAGE_COUNT = 3

# Max consecutive tool_call rounds before forcing a final answer. 防 LLM 卡在
# 工具循环里不出 action，浪费 token + 阻塞 step 推进。3 round 足够 Figma
# review / 业务 MCP 取一两个上下文，业务上少见超过 2 round。
MCP_TOOL_LOOP_LIMIT = 3

EXPLORATORY_PROMPT = """## 探索性测试模式 (Exploratory)

你处于探索性测试模式。**不要只机械验证 Then 列出的断言**，请同时主动寻找问题：

1. **以真实用户视角操作**：模拟一个新手用户会做什么——读完不点直接返回、犹豫、误触、想搜不存在的东西、把窗口缩小、…
2. **主动挖掘以下类型问题**（即使用例没明示）：
   - 视觉：错位、重叠、文字截断、对比度不足、加载抖动、错位的国际化
   - 交互：按钮点不到、tap 区域过小、键盘遮挡输入框、双击触发两次、…
   - 文案：错别字、中英文混杂、占位文案没替换、报错信息无意义
   - 性能：明显卡顿、加载超过 3 秒无反馈、动画掉帧
   - 可用性：路径不直觉、缺少返回、空状态体验糟糕、操作无反馈
3. **观察"沉默的失败"**：操作执行了但效果不符合期望（如按钮点了但没反应、表单提交了但没确认）
4. **最终 done.reason 必须包含**：
   - 用例 Then 各项的 PASS/FAIL（基础回归判断）
   - 探索中额外发现的问题清单（分类列出 + 严重度估计：critical / major / minor）
   - 如果一切正常，明确写「未发现额外问题」"""

SHARED_SYSTEM_PROMPT = """你是一个专业的 QA 测试工程师 Agent。{platform_description}

你可以看到当前屏幕截图和 UI/DOM 元素树（a11y XML）。根据测试用例描述，你需要：
1. 理解当前屏幕状态
2. 决定下一步操作（一次只出一个 action）
3. 判断**当前 step** 的状态：still in_progress / pass / fail

返回 JSON 格式：
{{
    "observation": "对当前屏幕的观察描述",
    "thinking": "你的思考过程",
    "step_progress": {{
        "current_step_index": <int, 你**这一轮正在推进**的 step 序号 (1-based, Background 不计)。必须 = 上一轮的 current_step_index，或 = 上一轮的 current_step_index + 1。**任何跳跃都会被 reject 强制重做**>,
        "current_step_status": "<in_progress | pass | fail>",
        "evidence": "<当 current_step_status = pass 或 fail 时**必填**：当前截图里能验证该 step 结论的具体证据。必须提到屏幕上能看到的具体元素（按钮名、文字、页面名、颜色、位置、弹窗、图标等），且至少 15 字符。例：「页面顶部出现「设置」标题，列表第一项「账号与安全」可见」。光说「已完成 / 不通过」会被 reject。**禁止使用「假设通过 / 后台逻辑 / 视觉无法验证但符合预期 / 是 mock 设定 / 推断成立」这类话术蒙混 PASS** — 看不见的断言直接 status=fail，详见下方第 5 条>",
        "fail_reason": "<当 current_step_status = fail 时必填：为什么这一步未达预期，引用 step 文本里的具体断言点>"
    }},
    "action": {{
        "type": "<action_type>",
        // action 参数见下方平台说明
        // 当 current_step_status = pass 且这是最后一个 step 时，type 用 "done" 结束
        // 当 current_step_status = fail 时，type 也用 "done"（agent 会终止整 scenario）
    }}
}}

{platform_prompt_segment}

## BDD Scenario 阅读规范（测试用例文本遵循 Cucumber-Gherkin 语法）
- 用例文本一个 Scenario 描述**单一行为**，按 step 顺序 chronological 执行
- **Background** 段已由 argus 通过 Reset 模式实现为 fixture（pm_clear / relaunch）。Background 的 Given 步骤代表 fixture 应满足的状态，**通常你不需要重新执行**，但若当前屏幕与 Background Given 描述明显不符（如 Background 说"已落地 Home Feed For You Tab"但当前是登录页）：
  - **先检查测试用例文本最前面是否有 `# <项目名> 测试前置状态恢复指南`（或类似标题）的章节**——这是 argus 自动 prepend 的"如何从异常状态恢复到 Background 描述状态"的说明。若有，**按指南先恢复**（判断当前状态 → 执行恢复操作 → 验证已到正确状态）再开始测试主体；恢复期间的 action 不计入 step_progress，但请在 thinking 里说明"正在做前置恢复"。
  - 若**没有**恢复指南，或按指南连续两次恢复仍失败，把 current_step_index=1 / current_step_status=fail / fail_reason="fixture 未就位 / 恢复失败：<具体描述>" + action.type=done。
- **Given** = Arrange（前置状态）。Scenario 内部的 Given 描述测试开始时应有的额外状态，通常无需操作，只需观察确认
- **When** = Act（用户实际动作）。这是你要在屏幕上触发的操作，按字面执行（"点击 X 按钮"→tap X 中心；"从顶部向下拖拽屏幕高度的 1/4"→swipe down ~25% screen height）
- **Then** = Assert（可观察结果）。每条 Then/And/But 都是**独立的视觉断言**，必须逐条核对当前屏幕；**任一条未满足整 Scenario 即 fail**
- **But** 通常表达否定断言（"不出现 X"），需要在屏幕上确认 X 真的不存在才算通过
- **And** 延续上一条 step 类型，不切换阶段
- step 用 domain language（用户视角）描述行为，不含 UI 实现细节（selector/XPath/CSS）；按业务语义理解

## ⚠️ Step 推进硬约束（违反会被 agent 拒绝并强制重做）

**你会看到整个 Scenario 的全部 step 列表，这是为了让你理解测试上下文 / 终极目标 / 业务逻辑。但你的输出空间被严格限制：**

1. **`current_step_index` 单调推进，禁止跳跃**
   - 只能 = 上一轮 + 0（当前 step 还没完成，继续执行 sub-action） 或 + 1（当前 step 刚 pass，进入下一 step）
   - 例：上一轮 current_step_index=2，这一轮只能 2 或 3，**不能** 4/5/6
   - 你不许「偷瞄未来 step 的终态」然后宣布提前完成。即使屏幕看起来已经到了终局状态，也必须**逐步**推进 current_step_index 一次一格

2. **`evidence` 必须基于「当前截图」直接可见的元素**
   - 不许引用「我记得之前看到」、「应该是」、「按推测」这类间接说法
   - 不许复述 step 文本——evidence 是**屏幕看到的实际内容**，跟 step 期望对比后得出结论
   - 至少提及一个屏幕元素：按钮名 / 文字内容 / 页面标题 / 弹窗 / 颜色 / 位置（顶部/底部/左/右）/ 图标 等

3. **`current_step_status = pass` 的判定条件**
   - 当前 step 是 When/动作类：屏幕已出现该动作的可见效果（如点击后页面跳转、输入后字段显示文字）
   - 当前 step 是 Then/断言类：当前截图能逐项核对该 step 描述的视觉特征
   - 仅满足以上一项才可标 pass。**模棱两可一律继续 in_progress**

4. **`current_step_status = fail` 的判定条件**
   - 连续 2-3 次 sub-action 后仍无法达成 step 期望
   - 或截图明确出现与 step 期望相反的状态（如 Then 说「不出现错误提示」，截图却有错误弹窗）
   - fail 后 action.type = "done"，agent 会终止整个 scenario（Cucumber 标准：一步 fail 全 scenario fail）

5. **🚫 不可视觉验证的 step → 必须标 fail 而非 pass**

   当 step 文本含以下「**不在 App 屏幕内能直接看到**」的断言时，**禁止 status=pass**，
   必须 status=fail + fail_reason 标 "unverifiable: <类型描述>"。**绝不允许**用
   「假设通过 / 后台逻辑 / 推断成立 / 视觉无法直接验证但符合预期 / 这是 mock 设定不影响 UI」
   这类话术蒙混 PASS。

   不可验证类型：
   - **埋点 / 上报** — `上报 N_XXX_Click`、`browseEvent`、`埋点验证`、`track event`
   - **桌面 / Launcher** — `桌面图标`、`badge 数字`、`launcher icon`、`角标`（在 App 之外，截图看不到）
   - **系统时间** — `当前时间 / 设备时间 / now() 到达 XX:XX`（除非 App 内 UI 显示当前时间）
   - **storage / SharedPreferences flag** — `storage flag 写入`、`SharedPreferences key=X`（看不见持久化层）
   - **后端 API / 接口调用** — `调用 /v1/xxx`、`POST request 发起`、`后端 mock 返回 X`
   - **内部 method / store / route** — `Navigator.push`、`XxxStore.method()`、`XxxRoute` 路由名
   - **HapticFeedback / 震动** — 物理反馈不可视
   - **系统通知抽屉 / 系统设置页** — Android 通知中心、Settings App（在 NewsBang App 之外）
   - **跨 App / Deeplink 目标 App** — 如「跳转到 Gmail」「打开 Maps」（已离开被测 App）

   遇到这类 step：
   - **如果 case 只是这一个 step 是不可视的，整 case 仍能跑** → 该 step status=fail + fail_reason=
     "unverifiable: <类型>，需 case 加 @skip-vision tag 或换成 UI 后果断言"
   - **整 case 全是不可视断言** → step 1 直接 fail，reason 同上
   - **可视断言 + 不可视断言混合** → 验证可视的、跳不可视的部分给出 fail
     （注意：cucumber 一步 fail = scenario fail，所以含不可视 step 的 case 必 fail，
      这是正确行为；防止 LLM 自欺给假 PASS。case 改写时这种 step 应被打 @skip-vision 跳过）

   **判断口诀**："我能在当前截图里直接看到吗？看不到就 fail，不要替它编理由"

## 坐标定位规范（关键 — 错了 tap 就空点）
坐标来源按优先级排序，**找到匹配就用，不要再视觉估算**：

1. **UI/DOM 元素树（最高优先级）** — 上方「UI/DOM 元素树」段落里如果有节点的 `text` / `content-desc` / `resource-id` / `name` / `label` 跟你想点的目标匹配（且 `clickable="true"` 或 iOS 上 `enabled`），**必须**直接用该节点的 `bounds` 中心点：
   - Android bounds 格式 `[x1,y1][x2,y2]` → 中心 `((x1+x2)/2, (y1+y2)/2)`
   - iOS frame `{{x,y,w,h}}` → 中心 `(x+w/2, y+h/2)`
   - 找到匹配节点后**忽略截图视觉估算**，UI tree 的 bounds 是 OS 上报的精确像素坐标，你的视觉模型对小元素 y 坐标有 ~50px 系统性偏差，估出来的坐标往往落在按钮 bounds 外导致 tap 空点。
   - 注意区分**目标元素本身**和**它的父容器**：一个父级 View 包多个子元素时 bounds 是整个容器范围，不是按钮本身——优先选 `clickable="true"` 且 `content-desc` 最匹配的叶子节点。

2. **element_marker skill 编号** — 如果截图上画了数字编号 + 一并喂了「编号→bounds」表，按编号查表取中心，不要看屏幕估。

3. **视觉估算（兜底）** — 仅在 UI tree 和 marker 都无匹配节点时使用（如 Flutter 自绘装饰、Canvas 内子元素、纯图像区域）。此时按以下要点：
   - 坐标系：左上角 (0,0)，向右 x 增大，向下 y 增大；坐标必须落在屏幕尺寸范围内
   - 对小元素（顶部图标、关闭按钮等）仔细估中心点，注意 y 方向可能有偏差
   - 测试用例里如有「Hints」或「位置参考」段落（"右上角"、"X 按钮左边"），把它当辅助线索缩小搜索范围

**自检**：决策前问自己「UI tree 里有没有匹配的 clickable 节点？」——有就用 bounds 中心，**不要**用「视觉中心约为 (540, 1970)」这种估算，否则大概率空点。

## 软键盘 / IME 处理（关键 — 防键盘遮挡盲点）
当上方上下文里出现 `## 键盘状态` 段（keyboard_detector skill 报告），意味着软键盘正占据屏幕底部某 y 起的区域（`y_start`）。**必须遵守**：

1. **第一硬规定**：若你打算 tap 的目标坐标 `y >= y_start`（即目标落在键盘区域），**禁止直接 tap**——目标在键盘后面，tap 命中的是键盘字母键，必然空点。
   - 必须先收键盘：`{{"type": "press_key", "key": "back"}}`（Android）或 `{{"type": "press_key", "key": "escape"}}`（iOS / 通用）
   - 收起键盘后下一轮再 tap 真正的目标。

2. **input 后想 tap 提交按钮 → 必须先收键盘**：上一个 sub-action 是 `input`、当前 step 还要点位于输入框下方的按钮（Continue/Submit/确认/下一步/Send/Done 等）→ 先 `press_key: back` 收键盘，下一轮再 tap。`input` 触发的 IME 不会自动消失。

3. **no_effect 反馈 + 键盘可见 = 100% 键盘遮挡**：上一 tap 被标 `⚠️ 该动作未产生任何可见变化` 且当前仍有 `## 键盘状态` → **禁止**再 tap 同位置，**必须**先 dismiss IME。重复同坐标 tap 是浪费 sub-action 配额。

**反例**（实测）：输入邮箱后想 tap (540, 810) 处的 Continue 按钮，但键盘 `y_start=720`，所以 (540, 810) 落在键盘上 → tap 命中键盘字母键 → change=0% → 又 tap 同坐标 → 死循环 → 撞 10 次 sub-action 限额。**正确**：先 `press_key: back` 收键盘 → 看到键盘消失 → 再 tap (540, 810)。
- 你会同时看到多张截图：近期 1–3 张历史截图（按时间顺序在前）+ 1 张当前截图（在最后）
- **必须以"当前截图"为唯一决策依据**，历史截图只用来直观感受变化；不要被自己之前写的 observation 文字描述带偏——如果当前截图明显跟历史描述不一致，相信当前截图
- 每次只返回一个 action
- 当所有 step 完成（最后 step 的 current_step_status = pass）→ action.type = "done"，result = "pass"
- 任一 step fail → action.type = "done"，result = "fail"（Cucumber 标准，后续 step 由 agent 标 skip 不再调用你）
- 只返回 JSON，不要包含其他内容"""


def _build_system_prompt(platform) -> str:
    """Build system prompt by combining shared prefix + platform segment."""
    if platform is None:
        platform_description = "你正在操作一个 iOS 模拟器来执行测试用例。"
        platform_segment = ""
    else:
        platform_description = platform.get_system_prompt_segment().split("\n")[0]
        platform_segment = platform.get_system_prompt_segment()

    return SHARED_SYSTEM_PROMPT.format(
        platform_description=platform_description,
        platform_prompt_segment=platform_segment,
    )


def _extract_json(raw: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences and stray text."""
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1]
        text = text.split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1]
        text = text.split("```", 1)[0]
    text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


# action.type 方言别名 → argus 规范 type
_ACTION_TYPE_ALIASES = {
    "click": "tap", "left_click": "tap", "tap_point": "tap", "touch": "tap",
    "double_click": "tap", "double_tap": "tap",
    "type": "input", "type_text": "input", "input_text": "input", "text": "input",
    "key": "press_key", "keypress": "press_key", "press": "press_key", "key_press": "press_key",
    "scroll": "swipe", "drag": "swipe", "swipe_action": "swipe",
}


def _coerce_xy(action: dict, dst_x: str = "x", dst_y: str = "y") -> bool:
    """把方言坐标(coordinate/position/point=[x,y])归一到 action[dst_x/dst_y]。
    已有 dst_x/dst_y 则不动。返回是否成功定位坐标。"""
    if dst_x in action and dst_y in action:
        return True
    for key in ("coordinate", "position", "point", "coord", "xy"):
        v = action.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            action[dst_x], action[dst_y] = int(v[0]), int(v[1])
            return True
    return False


def _normalize_action(action: dict) -> dict:
    """把 GUI-agent 类模型(Qwen2.5-VL 等)的动作方言翻译成 argus 规范 schema。

    这些模型受 grounding 训练强 prior 影响，倾向吐 {"type":"click","coordinate":[x,y]}
    而非 argus 约定的 {"type":"tap","x":..,"y":..}，不翻译会被 execute_action 当未知
    type / KeyError('x') 吞掉，每个动作变空操作。这里做无副作用的兼容映射。
    """
    if not isinstance(action, dict):
        return action
    raw_type = str(action.get("type", "")).lower().strip()
    norm_type = _ACTION_TYPE_ALIASES.get(raw_type, raw_type)
    action["type"] = norm_type

    if norm_type == "tap":
        _coerce_xy(action)
    elif norm_type == "swipe":
        # 起点 coordinate / 终点 coordinate2|to|end；或方言 direction 滚动
        direction = str(action.get("direction", "")).lower()
        if direction in ("up", "down") and "y1" not in action:
            action["type"] = "scroll_up" if direction == "up" else "scroll_down"
        else:
            if "x1" not in action:
                _coerce_xy(action, "x1", "y1")
            if "x2" not in action:
                for key in ("coordinate2", "to", "end", "target"):
                    v = action.get(key)
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        action["x2"], action["y2"] = int(v[0]), int(v[1])
                        break
    elif norm_type == "input":
        if "text" not in action:
            for key in ("value", "content", "input"):
                if key in action:
                    action["text"] = action[key]
                    break
    elif norm_type == "press_key":
        if "key" not in action:
            for key in ("keys", "value", "button"):
                if key in action:
                    action["key"] = action[key]
                    break
    return action


def _image_block(png_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(png_bytes).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
    }


class Brain:
    def __init__(self, llm_config: dict | None = None, platform=None,
                 mcp_registry=None):
        from .config import load_config
        cfg = llm_config or load_config()["llm"]
        # Optional headers (OpenRouter etiquette: HTTP-Referer + X-Title for
        # attribution on its leaderboard). Other providers ignore them.
        default_headers = cfg.get("extra_headers") or None
        self.client = OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=LLM_TIMEOUT_S,
            max_retries=0,  # brain.py 自己有 retry loop（LLM_MAX_RETRIES），关掉 SDK 内置 retry 避免叠加放大等待
            default_headers=default_headers,
        )
        self.model = cfg["model"]
        self.max_tokens = int(cfg.get("max_tokens") or 8192)
        self.history: list[dict] = []
        self.history_images: list[bytes] = []  # most recent last
        self.max_history_images = HISTORY_IMAGE_COUNT
        self.system_prompt = _build_system_prompt(platform)

        # MCP integration — brain 可调外部 MCP server 的 tool 来增强决策上下文
        # （如取 Figma frame 做视觉对比、查业务 API 等）。registry 不为空时
        # decide() 在主体 LLM 调用前会附 tools=… 并跑最多 MCP_TOOL_LOOP_LIMIT
        # 轮 tool_calls 交互。registry=None 时行为完全等同 MCP 接入前。
        self.mcp_registry = mcp_registry
        self._mcp_tools_cache: list[dict] | None = None
        self._mcp_tool_routing: dict[str, tuple[str, str]] = {}
        if mcp_registry is not None and mcp_registry.servers:
            self._load_mcp_tools()

    def _load_mcp_tools(self) -> None:
        """从 registry 拉 tool catalog，转 OpenAI tool schema 缓存。

        子进程 spawn 慢，所以只在 Brain 初始化时拉一次。运行期间 server 增减
        需要重启 Agent（与 argus 当前的"每跑一批重新 boot"工作流契合）。
        """
        from .mcp.client import to_openai_tools

        all_tools: list[dict] = []
        for server_name, tools in self.mcp_registry.list_all_tools_sync().items():
            for t in tools:
                full = f"{server_name}__{t['name']}"
                self._mcp_tool_routing[full] = (server_name, t["name"])
            all_tools.extend(to_openai_tools(tools, server_prefix=server_name))
        self._mcp_tools_cache = all_tools or None
        if all_tools:
            log.info("MCP 工具加载: %d tools across %d server(s)",
                     len(all_tools), len(self.mcp_registry.servers))

    def _invoke_mcp_tool(self, full_name: str, arguments: dict) -> str:
        """执行单个 MCP tool_call，返回喂回 LLM 的文本结果。"""
        routing = self._mcp_tool_routing.get(full_name)
        if routing is None:
            return f"[ERROR] unknown tool: {full_name}"
        server, name = routing
        try:
            result = self.mcp_registry.call_tool_sync(server, name, arguments)
        except Exception as e:
            log.warning("MCP tool 调用异常 %s/%s: %s", server, name, e)
            return f"[EXCEPTION] {e}"
        parts: list[str] = []
        for item in result.get("content", []):
            kind = item.get("type")
            if kind == "text":
                parts.append(item["text"])
            elif kind == "image":
                # 图片以占位描述给 LLM；具体多模态 attach 等业务真用时再扩展
                # （需要把 image base64 转 messages 里的 image_url block，
                # OpenAI tool message 不直接支持 image，得追加 user message）
                parts.append(f"[image {item.get('mime_type','image/png')}, "
                             f"len={len(item.get('data',''))} b64]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        text = "\n".join(parts) if parts else "[empty content]"
        if result.get("is_error"):
            text = f"[TOOL ERROR] {text}"
        return text

    def decide(self, test_case: str, screenshot_png: bytes, ui_tree: str,
               screen_size: tuple[int, int] = (402, 874),
               skill_context=None,
               scenario_steps: list[str] | None = None,
               current_step_index: int = 1,
               completed_evidence: list[str] | None = None,
               plan_hint: str = "",
               retry_feedback: str = "",
               use_grid: bool = False) -> dict | None:
        """Given a test case, screenshot, and UI tree, decide the next action.

        Args:
            skill_context: Optional SkillContext with enhanced image and supplementary text.
            scenario_steps: Full list of Scenario steps (without Background) — LLM sees
                them all to understand narrative, but is constrained to advance one at a time.
            current_step_index: Which step (1-based) LLM is currently working on. agent.py
                tracks this and rejects any decision that jumps ahead.
            completed_evidence: List of evidence strings, one per already-passed step,
                so LLM has anchor points for what's been verified.
            plan_hint: Pre-computed planner.expected_state / action_hint for the **current**
                step (highlighted in prompt). Empty string when planner unavailable.
            retry_feedback: If the previous decision was rejected by validator,
                the reject reason is fed back here so LLM corrects course.

        Returns the decision dict, or None if all retries failed.
        """
        w, h = screen_size

        # 默认发 RAW 截图（无网格/无标记），让 LLM 用原生视觉定位。
        # 例外：use_grid（多次 no_effect 卡住时由 agent 触发一次）→ 叠坐标网格兜底，
        # 让 LLM 照网格读精确像素坐标（复用 grid.draw_coordinate_grid）。
        if skill_context is not None and skill_context.raw_image is not None:
            if use_grid:
                base = skill_context.raw_image
                scale = base.width / w if w else 1.0
                current_png = img_to_png_bytes(
                    draw_coordinate_grid(base.copy(), scale, w, h))
            else:
                current_png = img_to_png_bytes(skill_context.raw_image)
        else:
            current_png = screenshot_png  # 无 raw_image 时降级原图（网格不可用）

        # Collect supplementary text from skills
        skills_text = ""
        if skill_context is not None:
            for name, result in skill_context.skill_results.items():
                if result.text:
                    skills_text += f"\n\n{result.text}"

        user_content: list[dict] = []

        # Recent history screenshots in chronological order (oldest → newest).
        recent_imgs = self.history_images[-self.max_history_images:]
        if recent_imgs:
            base_step = len(self.history) - len(recent_imgs) + 1
            user_content.append({
                "type": "text",
                "text": (f"下面是本测试用例最近 {len(recent_imgs)} 步的历史截图"
                         f"（仅供你直观感受变化，不要作为决策主体）："),
            })
            for offset, past_png in enumerate(recent_imgs):
                step_n = base_step + offset
                user_content.append({
                    "type": "text",
                    "text": f"### 历史截图 Step {step_n}",
                })
                user_content.append(_image_block(past_png))

        # Current screenshot — the authoritative one for this decision.
        current_step = len(self.history) + 1
        user_content.append({
            "type": "text",
            "text": f"### 当前截图 Step {current_step}（**请以此为准做决策**）",
        })
        user_content.append(_image_block(current_png))

        # Additional images emitted by skills (visual_diff highlight, toast crops, ...)
        if skill_context is not None:
            for name, result in skill_context.skill_results.items():
                for extra_png in result.extra_images:
                    user_content.append(_image_block(extra_png))

        # Detect exploratory mode from case metadata
        is_exploratory = (
            "Mode: exploratory" in test_case
            or "**Mode**: exploratory" in test_case
            or "Mode：exploratory" in test_case  # Chinese colon
        )
        mode_section = f"\n\n{EXPLORATORY_PROMPT}\n" if is_exploratory else ""

        # ── Step progress section — highlights current step in the full list ──
        step_progress_section = ""
        if scenario_steps:
            lines = ["## Step 推进状态（你的完整任务图谱）",
                     "",
                     "下面列出整个 Scenario 的所有 step。**全部都给你看是为了让你理解上下文和终极目标**，"
                     "但你这一轮**只能推进当前 step**（current_step_index 单调 +0 或 +1）。"
                     "禁止跳跃推进 — 即使屏幕看起来已经到了未来 step 的终态。",
                     "",
                     "Step 列表："]
            completed_evidence = completed_evidence or []
            for idx, step_text in enumerate(scenario_steps, start=1):
                if idx < current_step_index:
                    # already passed
                    ev = completed_evidence[idx - 1] if idx - 1 < len(completed_evidence) else ""
                    lines.append(f"  {idx}. ✅ [PASS] {step_text}")
                    if ev:
                        lines.append(f"     证据：{ev}")
                elif idx == current_step_index:
                    lines.append(f"  {idx}. 📍 **当前 step（你这一轮的目标）**：{step_text}")
                else:
                    lines.append(f"  {idx}. ⏳ [未来] {step_text}  ← 仅供理解 narrative，**禁止现在标 pass**")
            if plan_hint:
                lines += ["", "### Planner 对当前 step 的预判", plan_hint]
            step_progress_section = "\n".join(lines) + "\n\n"

        # ── 网格兜底提示（use_grid 时）──
        grid_section = ""
        if use_grid:
            grid_section = (
                "## 📐 坐标网格兜底（已多次点击无效，可能定位不准）\n"
                "本张截图**叠加了红色坐标网格**：每 100px 粗线带数字标签、50px 中线带小标签。\n"
                "请照网格读出目标元素的**精确像素坐标**(x, y) 再 tap，不要凭感觉估。\n"
                "网格只是辅助读数，目标元素本身位置不变。\n\n"
            )

        # ── Retry feedback — if previous decision was rejected ──
        retry_section = ""
        if retry_feedback:
            retry_section = (
                "## ⚠️ 上一次返回被 agent 拒绝（请修正后重出）\n"
                f"{retry_feedback}\n\n"
                "请重新返回符合约束的 JSON。\n\n"
            )

        # Text payload — test case, UI tree, skills text, full text history.
        user_content.append({
            "type": "text",
            "text": (
                f"{grid_section}"
                f"{retry_section}"
                f"## 屏幕尺寸\n宽={w}, 高={h}。"
                f"所有坐标必须在此范围内 (0 <= x <= {w}, 0 <= y <= {h})。\n\n"
                f"## 测试用例\n{test_case}\n"
                f"{mode_section}\n"
                f"{step_progress_section}"
                f"## UI/DOM 元素树 (精简)\n{_simplify_ui_tree(ui_tree)}\n\n"
                f"{skills_text}\n\n"
                f"## 历史操作 (完整)\n{self._format_history()}\n\n"
                f"请基于**当前截图**决定下一步操作。记住：current_step_index 只能 +0 或 +1。"
            ),
        })

        # MCP tool 注入说明（仅在有 tools 时加，避免污染默认行为）。
        mcp_section = ""
        if self._mcp_tools_cache:
            tool_lines = [f"  - {full}: {self._mcp_tool_routing[full][0]}/"
                          f"{self._mcp_tool_routing[full][1]}"
                          for full in self._mcp_tool_routing]
            mcp_section = (
                "\n\n## 可调用的外部 MCP 工具\n"
                "若需要补充上下文（如查 Figma 设计、查业务接口），可先发 tool_calls 取数据；"
                f"取完后**必须返回符合上文 JSON 结构的 action**。最多 {MCP_TOOL_LOOP_LIMIT} 轮 tool 交互。\n"
                + "\n".join(tool_lines)
            )
        if mcp_section:
            # 把 MCP 说明附在最后一条 text 上而不是新加 part —— 减少消息切碎
            user_content[-1]["text"] = user_content[-1]["text"] + mcp_section

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]

        for attempt in range(1, LLM_MAX_RETRIES + 1):
            try:
                raw, final_msg = self._call_with_tools(messages)
                decision = _extract_json(raw)
                if isinstance(decision.get("action"), dict):
                    decision["action"] = _normalize_action(decision["action"])

                self.history.append({
                    "observation": decision.get("observation", ""),
                    "action": decision["action"],
                })

                # Push current screenshot into history for next turn's context.
                self.history_images.append(current_png)
                # Keep only what's needed plus a small buffer.
                cap = self.max_history_images + 5
                if len(self.history_images) > cap:
                    self.history_images = self.history_images[-self.max_history_images:]

                return decision

            except json.JSONDecodeError as e:
                log.warning("JSON 解析失败 (第%d次): %s\n原始响应: %s", attempt, e,
                            raw[:200] if 'raw' in dir() else "N/A")
            except Exception as e:
                log.warning("LLM 调用失败 (第%d次): %s", attempt, e)

            if attempt < LLM_MAX_RETRIES:
                time.sleep(1)

        log.error("LLM 决策在 %d 次尝试后失败", LLM_MAX_RETRIES)
        return None

    def _call_with_tools(self, messages: list[dict]) -> tuple[str, object]:
        """Run the LLM call, dispatching any tool_calls before extracting JSON.

        Returns (content_text, last_message). 当 MCP tools 未启用时退化为
        单次 chat.completions.create + response_format=json_object，行为完全
        等同接入 MCP 前。
        """
        # 无 MCP tools：保持单 round + 强制 json_object（兼容 MCP 接入前路径）
        if not self._mcp_tools_cache:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=messages,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content, response.choices[0].message

        # 有 MCP tools：跑 tool_call 循环。
        # response_format 不与 tools 强制叠加 — 部分供应商对组合不稳；最终一轮
        # 我们用 _extract_json() 容错解析 markdown / 多余文本。
        msg = None
        for tool_round in range(MCP_TOOL_LOOP_LIMIT):
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=messages,
                tools=self._mcp_tools_cache,
            )
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                return msg.content or "", msg

            # 把 assistant 的 tool_calls turn 加进消息历史（OpenAI 协议要求 tool
            # response 之前必须有携带 tool_calls 的 assistant message）
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("MCP tool 调用 [%s] %s args=%s",
                         tool_round + 1, tc.function.name,
                         json.dumps(args, ensure_ascii=False)[:200])
                result_text = self._invoke_mcp_tool(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text[:8000],  # 兜底防超长 tool 结果撑爆 ctx
                })

        # 触顶仍未出 final answer — 强制再要一次（不附 tools 防再循环）
        log.warning("MCP tool_call 触达上限 %d，强制 LLM 出 final action",
                    MCP_TOOL_LOOP_LIMIT)
        messages.append({
            "role": "user",
            "content": (
                f"已达 MCP tool 调用上限 {MCP_TOOL_LOOP_LIMIT} 轮。"
                "现在请基于已收集到的信息**立即返回 JSON action**，不要再发 tool_calls。"
            ),
        })
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content, response.choices[0].message

    def _format_history(self) -> str:
        if not self.history:
            return "无（这是第一步）"
        lines = []
        for i, h in enumerate(self.history, 1):
            line = f"{i}. {h['observation']} → {json.dumps(h['action'], ensure_ascii=False)}"
            if h.get("focused_input"):
                line += ("  ✅ 该 tap 唤起了软键盘=已成功聚焦输入框（像素变化虽小但有效，"
                         "**不要再点别处/重复点**，下一步直接用 input 动作输入文字）")
            elif h.get("no_effect"):
                line += "  ⚠️ 该动作未产生任何可见变化（点击可能落空/被遮挡/坐标偏差，请勿重复同一坐标）"
            lines.append(line)
        return "\n".join(lines)

    def reset(self):
        self.history.clear()
        self.history_images.clear()


_UI_TREE_MAX_CHARS = 8000
_UI_TREE_KEEP_ATTRS = (
    "text", "content-desc", "resource-id", "class",
    "clickable", "enabled", "bounds",
)


def _simplify_ui_tree(xml_source: str) -> str:
    """**唯一**的 UI tree → LLM-facing 文本的简化入口（android.py 不再预简化，
    只返回原始树；dialog_dismisser / is_ime_visible 直接吃原始树）。

    Android uiautomator XML：结构化压缩 —— 见 ``_compact_android_xml``。无论大小
    都压缩，保证格式一致。非 Android（iOS plist / browser DOM）走 char-truncate。
    """
    compacted = _compact_android_xml(xml_source)
    if compacted is not None:
        if len(compacted) <= _UI_TREE_MAX_CHARS:
            return compacted
        # clickable 节点在前，超长时截尾（保住可点目标）。
        return compacted[:_UI_TREE_MAX_CHARS] + "\n... (truncated)"
    if len(xml_source) <= _UI_TREE_MAX_CHARS:
        return xml_source
    return xml_source[:_UI_TREE_MAX_CHARS] + "\n... (truncated)"


def _node_box(bounds: str) -> tuple[int, int, int, int] | None:
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _short_rid(rid: str) -> str:
    """``com.example.app:id/btn_login`` → ``btn_login``。"""
    return rid.split(":id/", 1)[1] if ":id/" in rid else rid


def _compact_android_xml(xml_source: str) -> str | None:
    """把 Android uiautomator XML 压成「可点目标 + 文字标签」的精简列表。

    简化规则（收拢到此一处）：
      1. **合并 clickable 父 + 其内部文字子**为一行 —— 直接把可点元素的可见文字
         作为 text，解决「文字在子节点、点击在父节点」导致 LLM 认不出该点哪的问题。
         按面积从小到大认领，内层具体按钮优先拿到自己的文字。
      2. **剥掉 resource-id 的包名前缀**（`com.x:id/foo` → `foo`），砍掉 class 噪音。
      3. 只保留 clickable 节点 + 未被任何 clickable 吞掉的独立文字节点（供断言）。

    返回 None 表示输入不像 Android 树（iOS/browser）或退化树（Flutter：无 clickable
    且无文字节点）→ 调用方回退原样/截断，对 Flutter 行为不变。
    """
    if "<hierarchy" not in xml_source and "<node" not in xml_source:
        return None

    # 属性值可能含 '/'（resource-id），故匹配到 '>' 为止，再忽略尾部自闭合斜杠。
    node_pat = re.compile(r"<node\b([^>]*)>", re.DOTALL)
    attr_pat = re.compile(r'(\w[\w-]*)="([^"]*)"')

    nodes = []
    for m in node_pat.finditer(xml_source):
        a = dict(attr_pat.findall(m.group(1)))
        a["_box"] = _node_box(a.get("bounds", ""))
        nodes.append(a)

    def label_of(a: dict) -> str:
        return a.get("text") or a.get("content-desc") or ""

    def contains(o, i) -> bool:
        return o[0] <= i[0] and o[1] <= i[1] and o[2] >= i[2] and o[3] >= i[3]

    def area(box) -> int:
        return (box[2] - box[0]) * (box[3] - box[1])

    clickables = [a for a in nodes if a.get("clickable") == "true" and a["_box"]]
    text_nodes = [a for a in nodes if label_of(a) and a["_box"]]

    # 退化树（Flutter 等）：无可点、无文字 → 交回调用方原样处理
    if not clickables and not text_nodes:
        return None

    # 小元素优先认领文字标签（内层具体按钮先于大容器）
    clickables.sort(key=lambda a: area(a["_box"]))
    consumed: set[int] = set()
    clickable_lines: list[str] = []
    for c in clickables:
        labels = []
        own = label_of(c)
        if own:
            labels.append(own)
        for t in text_nodes:
            if t is c or id(t) in consumed:
                continue
            lt = label_of(t)
            if lt and t["_box"] and contains(c["_box"], t["_box"]):
                labels.append(lt)
                consumed.add(id(t))
        label = " ".join(dict.fromkeys(labels))  # 去重保序
        if len(label) > 120:
            label = label[:120] + "…"
        parts = []
        if label:
            parts.append(f'text="{label}"')
        parts.append(f'bounds="{c.get("bounds", "")}"')
        parts.append('clickable="true"')
        rid = _short_rid(c.get("resource-id", ""))
        if rid:
            parts.append(f'id="{rid}"')
        if c.get("enabled") == "false":
            parts.append('enabled="false"')
        clickable_lines.append("<node " + " ".join(parts) + "/>")

    # 独立文字节点（未被 clickable 吞、自身不可点）—— 供断言用
    text_lines: list[str] = []
    for t in text_nodes:
        if id(t) in consumed or t.get("clickable") == "true":
            continue
        lt = label_of(t)
        parts = [f'text="{lt[:120]}"', f'bounds="{t.get("bounds", "")}"']
        rid = _short_rid(t.get("resource-id", ""))
        if rid:
            parts.append(f'id="{rid}"')
        text_lines.append("<node " + " ".join(parts) + "/>")

    header = (
        "<!-- 已结构化压缩：clickable 节点已合并其内部文字为 text；id 为去前缀的 "
        "resource-id。点击任意元素请取其 bounds 中心，不要视觉估算。-->\n"
    )
    return header + "\n".join(clickable_lines + text_lines)
