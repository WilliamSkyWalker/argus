"""Figma operations — gen-tests and visual review powered by LLM.

Client 选择由 ``figma_via_mcp.get_figma_client`` 统一决定：``.argus/mcp_clients.json``
里注册了 ``figma`` server 就走 MCP（无需 FIGMA_TOKEN）；没注册就回落到 REST
（需要 FIGMA_TOKEN）。两条路径返回数据形状一致，本模块代码不用区分。
"""

import base64
import json
import time

from openai import OpenAI

from .figma import FigmaNode, node_to_summary, parse_figma_url
from .figma_via_mcp import get_figma_client
from .logger import get_logger

log = get_logger("figma_ops")


# ── 1. Design → Test Cases ────────────────────────────────────

GEN_TESTS_SYSTEM_PROMPT = """你是一个资深 QA 测试工程师。你的任务是根据 Figma 设计稿的 UI 结构，生成可执行的自然语言测试用例。

输入：一个 Figma 页面/帧的 UI 元素结构树（包含元素类型、名称、文字内容、尺寸、颜色等）和对应的设计截图。

输出要求：
- 为每个核心交互流程生成一条测试用例
- 测试用例用自然语言描述，包含具体的操作步骤和预期结果
- 覆盖：正常流程、边界情况、异常输入
- 格式为 YAML Scenario 列表

输出格式：
```yaml
- scenario: "场景名称"
  steps: "详细的操作步骤和预期结果描述，可以直接交给 AI Agent 执行"

- scenario: "场景名称"
  steps: "..."
```

注意：
- 测试步骤要具体，包含要操作的元素名称/文字
- 预期结果要可验证（"看到XX"、"跳转到XX"、"显示XX"）
- 不需要写代码，纯自然语言即可
- 只输出 YAML，不要其他内容"""


def gen_tests_from_figma(figma_token: str, figma_input: str,
                          llm_config: dict) -> str:
    """Generate test cases from a Figma design.

    Args:
        figma_token: Figma personal access token（仅 REST fallback 用）
        figma_input: Figma URL or "file_key:node_id"
        llm_config: LLM configuration dict

    Returns:
        Generated test cases as YAML string.
    """
    client = get_figma_client(figma_token)
    file_key, node_id = _resolve_input(figma_input)

    # If no node specified, let user pick from available frames
    if not node_id:
        frames = client.list_frames(file_key)
        if not frames:
            raise RuntimeError("No frames found in this Figma file.")
        log.info("找到 %d 个帧，将为所有帧生成测试用例", len(frames))
        return _gen_tests_multi(client, file_key, frames, llm_config)

    return _gen_tests_single(client, file_key, node_id, llm_config)


def _gen_tests_single(client, file_key: str,
                       node_id: str, llm_config: dict) -> str:
    """Generate tests for a single frame."""
    log.info("正在提取帧结构: %s", node_id)
    structure = client.extract_structure(file_key, node_id)
    summary = node_to_summary(structure)

    log.info("正在导出设计截图...")
    png_bytes = client.export_png(file_key, node_id)
    png_b64 = base64.standard_b64encode(png_bytes).decode()

    log.info("正在调用 LLM 生成测试用例...")
    llm = OpenAI(api_key=llm_config["api_key"], base_url=llm_config["base_url"])

    response = llm.chat.completions.create(
        model=llm_config["model"],
        max_tokens=4096,
        messages=[
            {"role": "system", "content": GEN_TESTS_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
                {"type": "text",
                 "text": (f"## 帧名称\n{structure.name}\n\n"
                          f"## UI 元素结构\n{summary}\n\n"
                          f"请根据以上设计稿信息生成测试用例。")},
            ]},
        ],
    )

    result = response.choices[0].message.content
    # Strip markdown fences if present
    if "```yaml" in result:
        result = result.split("```yaml", 1)[1].split("```", 1)[0]
    elif "```" in result:
        result = result.split("```", 1)[1].split("```", 1)[0]

    return result.strip()


def _gen_tests_multi(client, file_key: str,
                      frames: list[dict], llm_config: dict) -> str:
    """Generate tests for multiple frames."""
    all_tests = []
    for frame in frames:
        log.info("处理帧: %s (%s)", frame["name"], frame["id"])
        try:
            tests = _gen_tests_single(client, file_key, frame["id"], llm_config)
            all_tests.append(f"# === {frame['name']} ===\n{tests}")
        except Exception as e:
            log.warning("帧 %s 处理失败: %s", frame["name"], e)
    return "\n\n".join(all_tests)


# ── 2. Visual Review: Design vs Screenshot ────────────────────

REVIEW_SYSTEM_PROMPT = """你是一个专业的 UI 视觉走查专家。你会收到两张图片：
1. **Figma 设计稿**（设计师的预期效果）
2. **实际截图**（App/网页的真实渲染效果）

请仔细对比两张图片，找出所有视觉差异，并生成走查报告。

检查维度：
- **布局**: 元素位置、间距、对齐方式是否一致
- **尺寸**: 元素大小是否匹配
- **颜色**: 背景色、文字颜色、边框颜色是否准确
- **字体**: 字号、字重、行高是否一致
- **文字内容**: 是否有拼写错误、缺失或多余的文字
- **图标/图片**: 是否正确显示、尺寸是否匹配
- **状态**: 按钮、输入框等交互元素的状态是否正确
- **圆角/阴影**: 是否与设计一致
- **响应式**: 是否有溢出、截断、换行异常

输出格式（JSON）：
{
    "score": 85,  // 0-100 还原度评分
    "summary": "整体还原度较好，主要问题在...",
    "issues": [
        {
            "severity": "high|medium|low",
            "category": "布局|颜色|字体|尺寸|内容|图标|其他",
            "description": "具体问题描述",
            "location": "问题所在区域"
        }
    ],
    "highlights": ["做得好的方面"]
}

只返回 JSON，不要其他内容。"""


def visual_review(figma_token: str, figma_input: str,
                   screenshot_png: bytes, llm_config: dict,
                   frame_name: str = "") -> dict:
    """Compare a Figma design with an actual screenshot.

    Args:
        figma_token: Figma personal access token
        figma_input: Figma URL or "file_key:node_id"
        screenshot_png: Actual app/browser screenshot as PNG bytes
        llm_config: LLM configuration dict
        frame_name: Optional frame name for context

    Returns:
        Review result dict with score, issues, etc.
    """
    client = get_figma_client(figma_token)
    file_key, node_id = _resolve_input(figma_input)

    if not node_id:
        frames = client.list_frames(file_key)
        if not frames:
            raise RuntimeError("No frames found. Specify a node-id in the URL.")
        node_id = frames[0]["id"]
        frame_name = frame_name or frames[0]["name"]
        log.info("未指定帧，使用第一个: %s (%s)", frame_name, node_id)

    log.info("正在导出 Figma 设计图...")
    design_png = client.export_png(file_key, node_id)
    design_b64 = base64.standard_b64encode(design_png).decode()
    screenshot_b64 = base64.standard_b64encode(screenshot_png).decode()

    log.info("正在调用 LLM 进行视觉对比...")
    llm = OpenAI(api_key=llm_config["api_key"], base_url=llm_config["base_url"])

    response = llm.chat.completions.create(
        model=llm_config["model"],
        max_tokens=4096,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"帧名称: {frame_name}\n\n图1 = Figma 设计稿, 图2 = 实际截图"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{design_b64}"}},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]},
        ],
    )

    raw = response.choices[0].message.content.strip()
    # Extract JSON
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    raw = raw.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

    result = json.loads(raw)
    result["design_png"] = design_png
    return result


def review_with_platform(figma_token: str, figma_input: str,
                          platform, llm_config: dict) -> dict:
    """Take a screenshot from the current platform and compare with Figma design."""
    screenshot_png = platform.screenshot_png()
    return visual_review(figma_token, figma_input, screenshot_png, llm_config)


# ── Helpers ───────────────────────────────────────────────────

def _resolve_input(figma_input: str) -> tuple[str, str | None]:
    """Resolve Figma input (URL or file_key:node_id) into (file_key, node_id)."""
    if "figma.com" in figma_input:
        return parse_figma_url(figma_input)
    elif ":" in figma_input:
        parts = figma_input.split(":", 1)
        return parts[0], parts[1] if len(parts) > 1 else None
    else:
        return figma_input, None
