"""Gherkin (.feature) parser for argus.

将 Cucumber/Gherkin 风格的 .feature 文件转换成 argus cli.py 可消费的 case 序列。

支持：
- Feature / Background / Scenario / Scenario Outline + Examples 全套语法
- @tag 解析（@P0/@auto/@reset:pm_clear/@TC-XXX-NNN 等约定 tag → metadata）
- Background steps 自动 prepend 到每个 Scenario
- Scenario Outline 按 Examples 行展开成 N 个独立 Scenario
- # argus-* 头部注释作为 feature 级元数据

render_case() 输出的 case body 格式与 cli.py 既有的 `**Reset before**: xxx`
markdown 正则兼容，因此 cli.py 的下游（_extract_reset_mode 等）无需改动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ── 正则：Gherkin 关键字（含中文 BDD 关键字以备后用）──
KEYWORD_FEATURE = re.compile(r'^\s*(Feature|功能):\s*(.*)$')
KEYWORD_BACKGROUND = re.compile(r'^\s*(Background|背景):\s*(.*)$')
KEYWORD_SCENARIO_OUTLINE = re.compile(r'^\s*(Scenario Outline|Scenario Template|场景大纲|场景模板):\s*(.*)$')
KEYWORD_SCENARIO = re.compile(r'^\s*(Scenario|Example|场景|例子):\s*(.*)$')
KEYWORD_EXAMPLES = re.compile(r'^\s*(Examples|Scenarios|例子|场景):\s*(.*)$')

# Step 关键字（开头匹配，支持英文 + 常见中文）
STEP_PREFIXES = (
    'Given ', 'When ', 'Then ', 'And ', 'But ', '* ',
    '假如 ', '当 ', '那么 ', '而且 ', '并且 ', '但是 ',
    '前提 ', '如果 ', '同时 ',
)

# Tag 行：整行仅含 @tag-name 或 @tag:value（允许多个 tag 同行）
TAG_LINE = re.compile(r'^\s*(@\S+(\s+@\S+)*)\s*$')
TAG_TOKEN = re.compile(r'@(\S+)')

# Examples 表格行：| col1 | col2 |
TABLE_ROW = re.compile(r'^\s*\|.+\|\s*$')

# 文件头 # argus-key: value 元数据
ARGUS_META = re.compile(r'^\s*#\s*argus-([\w-]+):\s*(.+?)\s*$')

# 占位符：<key>
OUTLINE_PLACEHOLDER = re.compile(r'<([^<>\s]+)>')


@dataclass
class Scenario:
    name: str
    tags: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # 原始 step 行（含 Given/When 前缀）
    line_no: int = 0  # 起始行号（便于错误定位）

    @property
    def tc_id(self) -> str | None:
        """从 tag 中提取 @TC-XXX-NNN，没找到返回 None。"""
        for tag in self.tags:
            if tag.startswith('TC-'):
                return tag
        return None

    @property
    def priority(self) -> str:
        for tag in self.tags:
            if tag in ('P0', 'P1', 'P2'):
                return tag
        return 'P2'

    @property
    def automation(self) -> str:
        for tag in self.tags:
            if tag in ('auto', 'partial', 'manual'):
                return tag
        return 'manual'

    @property
    def platform(self) -> str:
        for tag in self.tags:
            if tag in ('ios', 'android', 'both'):
                return tag
        return 'both'

    @property
    def reset_mode(self) -> str | None:
        """从 @reset:xxx 提取 reset 模式；返回 None 表示用 feature 级默认。"""
        for tag in self.tags:
            if tag.startswith('reset:'):
                return tag[len('reset:'):]
        return None

    @property
    def skip(self) -> bool:
        return 'skip' in self.tags or 'wip' in self.tags


@dataclass
class Feature:
    name: str = ''
    description: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    background_steps: list[str] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    source_file: str = ''

    @property
    def reset_default(self) -> str:
        """feature 头部 # argus-reset-default 注释指定的默认 reset 模式。"""
        return self.meta.get('reset-default', 'relaunch')


# ─────────────────────────────────────────────────────────
# 主解析函数
# ─────────────────────────────────────────────────────────

def parse_feature(text: str, source_file: str = '') -> Feature:
    """解析 .feature 文本为 Feature 对象。

    遵循 Cucumber Gherkin 语法约定：
    - tag 行紧贴 Scenario / Scenario Outline 上方
    - Background 在 Feature 描述之后、第一个 Scenario 之前
    - Examples 紧跟在 Scenario Outline 之后
    """
    feature = Feature(source_file=source_file)

    lines = text.splitlines()
    i = 0
    state = 'header'  # 'header' | 'description' | 'background' | 'scenario'
    pending_tags: list[str] = []
    current: Scenario | None = None
    outline_examples: list[dict[str, str]] | None = None  # 当前 Outline 累积的 Examples 行

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 文件头 argus 元数据注释
        m = ARGUS_META.match(line)
        if m:
            feature.meta[m.group(1)] = m.group(2)
            i += 1
            continue

        # 空行 / 普通注释
        if not stripped or (stripped.startswith('#') and not ARGUS_META.match(line)):
            i += 1
            continue

        # Tag 行（累积到 pending_tags 等待下一个 Scenario / Scenario Outline / Feature）
        if TAG_LINE.match(line):
            pending_tags.extend(TAG_TOKEN.findall(line))
            i += 1
            continue

        # Feature:
        m = KEYWORD_FEATURE.match(line)
        if m:
            feature.name = m.group(2).strip()
            feature.tags = pending_tags
            pending_tags = []
            state = 'description'
            i += 1
            continue

        # Background:
        m = KEYWORD_BACKGROUND.match(line)
        if m:
            state = 'background'
            i += 1
            continue

        # Scenario Outline:（注意顺序：必须先匹配 Outline 再匹配 Scenario，否则被 Scenario 吃掉）
        m = KEYWORD_SCENARIO_OUTLINE.match(line)
        if m:
            if current is not None:
                _flush_scenario(feature, current, outline_examples)
            current = Scenario(name=m.group(2).strip(), tags=pending_tags, line_no=i + 1)
            pending_tags = []
            outline_examples = []
            state = 'scenario'
            i += 1
            continue

        # Scenario:
        m = KEYWORD_SCENARIO.match(line)
        if m:
            if current is not None:
                _flush_scenario(feature, current, outline_examples)
            current = Scenario(name=m.group(2).strip(), tags=pending_tags, line_no=i + 1)
            pending_tags = []
            outline_examples = None
            state = 'scenario'
            i += 1
            continue

        # Examples:（仅在 Scenario Outline 下文有意义）
        m = KEYWORD_EXAMPLES.match(line)
        if m and outline_examples is not None:
            i = _consume_examples_table(lines, i + 1, outline_examples)
            continue

        # Step 行
        if any(stripped.startswith(prefix) for prefix in STEP_PREFIXES):
            if state == 'background':
                feature.background_steps.append(stripped)
            elif state == 'scenario' and current is not None:
                current.steps.append(stripped)
            i += 1
            continue

        # Data table 行（接在 step 后面）
        if TABLE_ROW.match(stripped):
            if state == 'background':
                feature.background_steps.append(stripped)
            elif state == 'scenario' and current is not None:
                current.steps.append(stripped)
            i += 1
            continue

        # Doc string """...""" 多行块
        if stripped.startswith('"""') or stripped.startswith("'''"):
            opener = stripped[:3]
            block = [stripped]
            i += 1
            while i < len(lines):
                block.append(lines[i].rstrip())
                if lines[i].strip().endswith(opener):
                    i += 1
                    break
                i += 1
            joined = '\n'.join(block)
            if state == 'scenario' and current is not None:
                current.steps.append(joined)
            elif state == 'background':
                feature.background_steps.append(joined)
            continue

        # Feature description（Feature: 和 Background:/Scenario: 之间的自由文本）
        if state == 'description':
            feature.description.append(stripped)

        i += 1

    # 收尾：flush 最后一个 scenario
    if current is not None:
        _flush_scenario(feature, current, outline_examples)

    return feature


def _consume_examples_table(lines: list[str], start: int, into: list[dict[str, str]]) -> int:
    """从 start 开始消费 Examples 表格，把每行（除 header）作为 dict 加入 into。

    返回 first non-table line 的索引。"""
    i = start
    header: list[str] | None = None
    while i < len(lines):
        s = lines[i].strip()
        if TABLE_ROW.match(s):
            cells = [c.strip() for c in s.strip('|').split('|')]
            if header is None:
                header = cells
            else:
                # 长度对齐（容错）
                row = {}
                for k, v in zip(header, cells):
                    row[k] = v
                into.append(row)
            i += 1
        elif not s or s.startswith('#') and not ARGUS_META.match(lines[i]):
            i += 1
        else:
            break
    return i


def _flush_scenario(feature: Feature, scenario: Scenario,
                    outline_examples: list[dict[str, str]] | None) -> None:
    """把累积的 Scenario flush 到 feature.scenarios。

    若是 Scenario Outline（outline_examples 非 None），按 Examples 行展开为多个 Scenario。"""
    if outline_examples is None:
        feature.scenarios.append(scenario)
        return

    # Outline 展开：每个 Examples 行生成一个 Scenario
    if not outline_examples:
        # Outline 但 Examples 为空 — 跳过（无可用例化数据）
        return

    for idx, row in enumerate(outline_examples, start=1):
        # Outline 展开：把 TC-* tag 加 -idx 后缀，保证 ID 在整 feature 内唯一
        expanded_tags: list[str] = []
        for t in scenario.tags:
            if t.startswith('TC-'):
                expanded_tags.append(f'{t}-{idx}')
            else:
                expanded_tags.append(t)
        expanded = Scenario(
            name=_substitute_outline(scenario.name, row),
            tags=expanded_tags,
            steps=[_substitute_outline(step, row) for step in scenario.steps],
            line_no=scenario.line_no,
        )
        feature.scenarios.append(expanded)


def _substitute_outline(text: str, row: dict[str, str]) -> str:
    """把 <key> 占位符替换为 Examples 行对应的 value。"""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        return row.get(key, m.group(0))  # 未找到 key 时保留原样

    return OUTLINE_PLACEHOLDER.sub(repl, text)


# ─────────────────────────────────────────────────────────
# Case body 渲染
# ─────────────────────────────────────────────────────────

def render_case(feature: Feature, scenario: Scenario) -> str:
    """把 Feature + Scenario 渲染成 argus cli.py 可消费的 case body。

    输出格式刻意兼容现有 cli.py 的 markdown 正则：
    - `**Reset before**: xxx` → cli._extract_reset_mode 识别
    - `open_url https://...` 出现在 step 里 → cli._extract_target_url 识别（browser 场景）

    格式示例：
        ### TC-FEED-002  For You Tab 下拉刷新替换推荐内容
        - **Priority**: P0
        - **Automation**: auto
        - **Platform**: both
        - **Reset before**: relaunch
        - **Feature**: Feed 流 - For You Tab 核心交互
        - **Background**:
          Given 测试账号 testuser 已登录
          ...
        - **When**:
          Given For You 列表首屏已加载完成
          When 用户从屏幕顶部向下拖拽约屏幕高度的 1/4
          Then 顶部 Tab Bar 下方出现一个圆形旋转 spinner
    """
    tc_id = scenario.tc_id or _fallback_id(feature, scenario)
    reset = scenario.reset_mode or feature.reset_default

    lines: list[str] = []
    lines.append(f"### {tc_id}  {scenario.name}")
    lines.append(f"- **Priority**: {scenario.priority}")
    lines.append(f"- **Automation**: {scenario.automation}")
    lines.append(f"- **Platform**: {scenario.platform}")
    lines.append(f"- **Reset before**: {reset}")
    if feature.name:
        lines.append(f"- **Feature**: {feature.name}")

    if feature.background_steps:
        lines.append("- **Background**:")
        for step in feature.background_steps:
            lines.append(f"  {step}")

    lines.append("- **Steps**:")
    for step in scenario.steps:
        lines.append(f"  {step}")

    return '\n'.join(lines)


def _fallback_id(feature: Feature, scenario: Scenario) -> str:
    """没有 @TC-XXX tag 时用文件名 + 行号兜底生成 ID。"""
    base = Path(feature.source_file).stem if feature.source_file else 'TC'
    return f"TC-{base}-L{scenario.line_no}"


# ─────────────────────────────────────────────────────────
# 文件 / 目录入口（argus cli.py 调用点）
# ─────────────────────────────────────────────────────────

def parse_feature_file(path: Path) -> list[tuple[str, dict]]:
    """解析单个 .feature 文件，返回 [(case_body, metadata), ...]。

    skip / wip tag 的 Scenario 会被过滤掉。"""
    text = path.read_text(encoding='utf-8')
    feature = parse_feature(text, source_file=str(path))

    results: list[tuple[str, dict]] = []
    for scenario in feature.scenarios:
        if scenario.skip:
            continue
        body = render_case(feature, scenario)
        meta = {
            'tc_id': scenario.tc_id or _fallback_id(feature, scenario),
            'priority': scenario.priority,
            'automation': scenario.automation,
            'platform': scenario.platform,
            'reset_mode': scenario.reset_mode or feature.reset_default,
            'feature_name': feature.name,
            'source_file': str(path),
            'tags': list(scenario.tags),
        }
        results.append((body, meta))
    return results


def parse_feature_dir(dir_path: Path, recursive: bool = True) -> list[tuple[str, dict]]:
    """递归解析目录下所有 .feature 文件，按文件名排序。"""
    pattern = '**/*.feature' if recursive else '*.feature'
    all_cases: list[tuple[str, dict]] = []
    for feature_file in sorted(dir_path.glob(pattern)):
        all_cases.extend(parse_feature_file(feature_file))
    return all_cases


def parse_feature_to_cases(text_or_path) -> list[str]:
    """便捷封装：返回纯 case body 列表（兼容 cli.py 现有 list[str] 接口）。

    text_or_path 可以是 Path、字符串路径、或者 inline .feature 文本。"""
    if isinstance(text_or_path, Path):
        return [body for body, _ in parse_feature_file(text_or_path)]
    text = str(text_or_path)
    # 启发式判断：含 "Feature:" / "Scenario:" 关键字才作为 .feature 文本，否则当成路径
    if 'Feature:' in text or 'Scenario:' in text:
        feature = parse_feature(text)
        return [render_case(feature, s) for s in feature.scenarios if not s.skip]
    p = Path(text)
    if p.exists():
        return [body for body, _ in parse_feature_file(p)]
    # 既不是合法 feature 文本也不是已存在路径 → 作为 inline 文本回退
    return [text]
