"""Test discovery, metadata filters, accounts and case placeholders."""

import json
import os
import re
import time
from pathlib import Path

from argus.config import PROJECT_ROOT
from argus.logger import get_logger


log = get_logger("qa.cases")


_OPEN_URL_RE = re.compile(r"open_url[^\n]*?(https?://\S+)", re.IGNORECASE)


_RESET_RE = re.compile(
    r"\*\*Reset before\*\*:\s*(pm_clear|relaunch|none)",
    re.IGNORECASE,
)


_PLATFORM_RE = re.compile(
    r"\*\*Platform\*\*:\s*([^\n]+)",
    re.IGNORECASE,
)


TESTS_DIR = PROJECT_ROOT / "tests"


def _extract_target_url(case_text: str) -> str | None:
    """Pull a target URL out of a scenario's text if it mentions open_url."""
    m = _OPEN_URL_RE.search(case_text)
    if not m:
        return None
    return m.group(1).strip().strip('"\'').rstrip(".,;)")


def _extract_reset_mode(case_text: str) -> str | None:
    """Return the value of `**Reset before**: <mode>` if present, else None."""
    m = _RESET_RE.search(case_text)
    return m.group(1).lower() if m else None


def _extract_platforms(case_text: str) -> set[str] | None:
    """解析 `**Platform**:` 行 → 平台集合(小写)。无该字段返回 None。

    值可为逗号/空格/斜杠分隔的多平台，如 `android, ios` / `android ios web`。
    """
    m = _PLATFORM_RE.search(case_text)
    if not m:
        return None
    return {t for t in re.split(r"[,\s/]+", m.group(1).strip().lower()) if t}


def _should_skip_by_platform(case_text: str, current_platform: str) -> str | None:
    """当前运行平台不在用例声明的平台集合里则 skip；否则跑。

    - 无 **Platform** 字段 / 值含 `any` / 遗留 `both` → 不限平台，跑。
    - 否则：当前平台在集合中才跑（browser 与 web 视为同一）。
    """
    plats = _extract_platforms(case_text)
    if not plats or "any" in plats or "both" in plats:
        return None
    cur = (current_platform or "").lower()
    if cur in plats or (cur == "browser" and "web" in plats) or (cur == "web" and "browser" in plats):
        return None
    return f"platform mismatch: case requires {'/'.join(sorted(plats))}, run is {cur}"


def _should_skip_by_automation(case_text: str) -> str | None:
    """Skip cases tagged @manual / @partial. These cases require real OAuth /
    real network / real device behavior that automation can't reliably check.
    Returns a skip reason or None.

    Detection: matches `**Automation**: manual` / `**Automation**: partial`
    field that gherkin.py emits from @manual / @partial tags.
    """
    m = re.search(r"^\s*-\s*\*\*Automation\*\*:\s*(\w+)", case_text, re.M)
    if not m:
        return None
    automation = m.group(1).lower()
    if automation in ("manual", "partial"):
        return f"automation mode '{automation}' — requires human review"
    return None


def _probes_mode() -> str:
    """`all`（默认）/ `skip` / `only` —— 由 `argus run --skip-probes|--only-probes` 设
    env `PROBES_MODE`（走 env 才能同时覆盖 --bg 子进程和多设备 worker）。"""
    mode = (os.environ.get("PROBES_MODE") or "all").strip().lower()
    return mode if mode in ("all", "skip", "only") else "all"


def _should_skip_by_probes(case_text: str) -> str | None:
    """`--only-probes` 时把「没有埋点断言」的 case 整个跳掉。

    注意这是 **case 级筛选**，不是「只跑 probe step」—— 埋点得靠前面的 UI 操作
    触发出来，只查库不操作 App 永远查不到东西。所以命中的 case 里 UI 步照常跑，
    只是没埋点断言的 case 不跑。想纯查库不跑 App 请用 `argus probes check`。
    """
    from argus.probes import has_probe_directive

    if _probes_mode() != "only":
        return None
    if has_probe_directive(case_text):
        return None
    return "no probe assertion (--only-probes)"


def _substitute_placeholders(case_text: str) -> str:
    """Substitute runtime placeholders in a case body.

    Currently supported:
      {epoch}  → Unix timestamp at run start (10 digits)
      {uuid}   → random 8-char hex (for unique-email cases)
    """
    import uuid as _uuid
    return (case_text
            .replace("{epoch}", str(int(time.time())))
            .replace("{uuid}", _uuid.uuid4().hex[:8]))


def _read_target_url(target_dir: Path) -> str | None:
    """Read the URL field from a target's README.md.

    Looks for patterns like:
      - **URL**: https://example.com
      - URL: https://example.com
      - [example.com](https://example.com)
    """
    import re
    readme = target_dir / "README.md"
    if not readme.exists():
        return None
    for line in readme.read_text().splitlines():
        # - **URL**: https://...
        m = re.search(r'\*\*URL\*\*\s*[:：]\s*(https?://\S+)', line)
        if m:
            return m.group(1).rstrip(")")
        # URL: https://...
        m = re.search(r'^[-\s]*URL\s*[:：]\s*(https?://\S+)', line, re.IGNORECASE)
        if m:
            return m.group(1).rstrip(")")
        # [text](https://...)  on first link-only line
        m = re.match(r'^\[.*?\]\((https?://\S+)\)', line.strip())
        if m:
            return m.group(1).rstrip(")")
    return None


def _load_accounts(target_dir: Path | None) -> list[dict]:
    """加载 tests 下顶级目录里的 `_accounts.json` 账号池（如有）。

    ⚠️ 定位：这个池**只承载两类东西** ——
      1. **密钥/凭据**（账号、密码）：不能写进 .feature（会进 git 泄敏），
         所以放 gitignored 的 _accounts.json，case 里用 `${EMAIL}` 等占位符引用。
      2. **并发互斥的可互换资源**：同一 case 同时在 N 台设备跑须各用不同账号。
    **普通测试数据（输入变体、不同入参→不同结果）请用 Gherkin `Examples` 表 /
    Data Table**（BDD 原生、gherkin.py 已支持、零代码），不要往这个池塞。

    多设备并发时按 --device 顺序分配（设备 1 → accounts[0]，设备 2 → accounts[1]，
    ...）由 dispatcher 按 worker_idx 直接绑定；单设备用 accounts[0]。不再有
    ARGUS_ACCOUNT_INDEX 之类的 env 透传。

    文件格式（JSON list）：
        [
          {"email": "${EMAIL_1}", "password": "${PASSWORD_1}"},
          {"email": "${EMAIL_2}", "password": "${PASSWORD_2}"}
        ]

    每个 dict 的所有键会被翻译成 `${KEY_UPPER}` 占位符在 case 文本里替换
    （见 _apply_account_placeholders）。约定常用键：email / password /
    phone / username / user_id 等。

    路径算法与 _load_preconditions 一致。文件缺失或解析失败时返回 []，
    不阻塞测试运行 — 用例里的占位符会原样保留，LLM 看到会注意到。
    """
    if target_dir is None:
        return []
    try:
        rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
    except ValueError:
        return []
    if not rel.parts:
        return []
    f = TESTS_DIR / rel.parts[0] / "_accounts.json"
    if not f.exists():
        return []
    try:
        import json
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            log.warning("_accounts.json 顶层应是 list，实际是 %s，忽略", type(data).__name__)
            return []
        # 每项必须是 dict
        accounts = [item for item in data if isinstance(item, dict)]
        if len(accounts) != len(data):
            log.warning("_accounts.json 含非-dict 项，已忽略")
        return accounts
    except Exception as e:
        log.warning("_accounts.json 解析失败: %s", e)
        return []


def _apply_account_placeholders(text: str, account: dict) -> str:
    """把 ``${EMAIL}`` / ``${PASSWORD}`` 等占位符替换为 ``account`` 里的值。

    替换规则：account 里每个键 ``foo`` → 占位符 ``${FOO}``（大写）。值为
    str/int/float 时直接 stringify 写入；其他类型跳过。

    无匹配键的占位符保留原样（LLM 看到会注意到异常，比静默替换更稳妥）。
    """
    if not account:
        return text
    for key, val in account.items():
        if not isinstance(val, (str, int, float)):
            continue
        text = text.replace(f"${{{key.upper()}}}", str(val))
    return text


def _load_preconditions(target_dir: Path | None) -> str | None:
    """加载 tests 下第一级目录里的 `_preconditions.md`（如有）。

    用例：tests/my-app/_preconditions.md 描述 "登录态怎么判断、不在登录态怎么登录、
    Onboarding 怎么完成、常见拦截弹窗怎么 dismiss"。运行时 prepend 到每个 case 文本前，
    让 LLM 在发现当前屏幕不符合 Background Given 时能照指南先恢复再开始测试。

    路径算法与 auto-report 一致：取 target_dir 相对 TESTS_DIR 的第一级目录名。
    """
    if target_dir is None:
        return None
    try:
        rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
    except ValueError:
        return None
    if not rel.parts:
        return None
    pre_file = TESTS_DIR / rel.parts[0] / "_preconditions.md"
    if not pre_file.exists():
        return None
    return pre_file.read_text(encoding="utf-8").rstrip()


def _find_target_dir(p: Path) -> Path | None:
    """从一个文件或目录路径向上回溯，找到最近的"含 README.md 的祖先目录"作为 target_dir。

    用于决定 report 目录位置等。约定一个 target 是含 README.md 的目录：
      - tests/web-demo/                 (README.md + cases/)
      - tests/my-app/mobile/            (README.md + 子模块/*.feature)
    """
    resolved = p.resolve()
    tests_resolved = TESTS_DIR.resolve()
    current = resolved if resolved.is_dir() else resolved.parent
    # 向上找直到 TESTS_DIR 之外
    while True:
        if (current / "README.md").exists():
            return current
        if current == tests_resolved or current == current.parent:
            break
        current = current.parent
    # 兜底：用 TESTS_DIR 下的第一个 path part 作为 target_dir
    try:
        rel = resolved.relative_to(tests_resolved)
        if rel.parts:
            return TESTS_DIR / rel.parts[0]
    except ValueError:
        pass
    return None


def _collect_feature_cases(path: Path) -> list[str]:
    """从一个 .feature 文件或含 .feature 的目录（递归）收集 case body。"""
    from argus.qa import gherkin
    if path.is_file() and path.suffix == ".feature":
        return gherkin.parse_feature_to_cases(path)
    if path.is_dir():
        cases: list[str] = []
        for ff in sorted(path.rglob("*.feature")):
            cases.extend(gherkin.parse_feature_to_cases(ff))
        return cases
    return []


def _resolve_test_target(test: str) -> tuple[list[str], Path | None]:
    """Resolve test argument to (test_cases, target_dir).

    支持（按检测顺序）：
      - "path/to/file.feature"          → 单 .feature 文件（Gherkin 解析）
      - "path/to/dir/" 或 "tests-rel"   → 已存在目录，递归找 .feature 文件
      - "tests-rel/file.feature"        → TESTS_DIR 相对路径下的 .feature 文件
      - "web-demo" / "web-demo/homepage" → 兼容旧 .md：tests/<name>/cases/
      - "path/to/file.md"               → 兼容旧 .md/.txt 单文件
      - "inline text"                   → 字面 case 文本
    """
    # 1) .feature 文件（绝对路径 / 相对当前目录 / TESTS_DIR 相对路径）
    if test.endswith(".feature"):
        candidates = [Path(test)]
        if not Path(test).is_absolute():
            candidates.append(TESTS_DIR / test)
        for candidate in candidates:
            if candidate.is_file():
                cases = _collect_feature_cases(candidate)
                return cases, _find_target_dir(candidate)
        # 明确是文件路径但不存在 — 直接报错，不能 fall through 变成 inline
        # LLM case（手滑打错路径会静默烧一次完整 LLM run）
        log.error("测试文件不存在: %s", test)
        raise SystemExit(f"test file not found: {test}")

    # 2) 已存在的目录路径（含 .feature 文件，递归）
    dir_candidates = [Path(test), TESTS_DIR / test]
    for candidate in dir_candidates:
        if candidate.is_dir():
            feature_cases = _collect_feature_cases(candidate)
            if feature_cases:
                return feature_cases, _find_target_dir(candidate)

    # 3) （兼容旧 .md 流）target/sub_name → tests/<target>/cases/<sub>.md
    if "/" in test and not test.endswith((".md", ".txt")):
        parts = test.split("/", 1)
        target_dir = TESTS_DIR / parts[0]
        case_file = target_dir / "cases" / f"{parts[1]}.md"
        if case_file.exists():
            with open(case_file) as f:
                return _parse_md_cases(f.read()), target_dir
        # Try as-is (maybe the user typed "web-demo/cases/foo.md")
        case_file = target_dir / parts[1]
        if case_file.exists():
            with open(case_file) as f:
                return _parse_md_cases(f.read()), target_dir

    # 4) （兼容旧）target 名字（无斜杠）→ tests/<target>/cases/*.md
    target_dir = TESTS_DIR / test
    if target_dir.is_dir() and (target_dir / "cases").is_dir():
        cases_dir = target_dir / "cases"
        all_cases = []
        for case_file in sorted(cases_dir.glob("*.md")):
            with open(case_file) as f:
                all_cases.extend(_parse_md_cases(f.read()))
        if all_cases:
            return all_cases, target_dir

    # 5) （兼容旧）显式 .md/.txt 文件路径
    if test.endswith((".md", ".txt")):
        test_path = Path(test)
        if not test_path.is_file() and not test_path.is_absolute() \
                and (TESTS_DIR / test).is_file():
            test_path = TESTS_DIR / test
        if not test_path.is_file():
            # 同上：路径打错不能裸 FileNotFoundError 也不能变 inline case
            log.error("测试文件不存在: %s", test)
            raise SystemExit(f"test file not found: {test}")
        with open(test_path) as f:
            cases = _parse_md_cases(f.read())
        resolved = test_path.resolve()
        if TESTS_DIR.resolve() in resolved.parents:
            rel = resolved.relative_to(TESTS_DIR.resolve())
            target_name = rel.parts[0] if rel.parts else None
            if target_name:
                return cases, TESTS_DIR / target_name
        return cases, None

    # 6) Inline case text
    return [test], None


def _parse_md_cases(text: str) -> list[str]:
    """Parse a TDD-style markdown test file into individual cases.

    Splits on `### TC-XXX` headers. Each case spans from its header to the
    next `### TC-` header or EOF.

    If the file contains a `## Hints` (or `## 元素位置 Hints`) section before
    the first case, that section is prepended to every case as shared context
    (useful for documenting hard-to-locate small UI elements once per file).

    Other text before the first `### TC-` header (intro, matrix table, etc.)
    is discarded.

    If no `### TC-` headers are present, the whole text is treated as one
    inline case (for use with ad-hoc cli runs).
    """
    cases = []
    current = None
    preamble_lines: list[str] = []

    def _is_case_header(line: str) -> bool:
        s = line.lstrip()
        return s.startswith("###") and "TC-" in s

    for line in text.splitlines():
        if _is_case_header(line):
            if current is not None:
                cases.append("\n".join(current).rstrip())
            current = [line]
        elif current is not None:
            current.append(line)
        else:
            preamble_lines.append(line)

    if current is not None:
        cases.append("\n".join(current).rstrip())

    if not cases:
        stripped = text.strip()
        return [stripped] if stripped else []

    # Extract a `## Hints` section from the preamble if present, and
    # prepend it to every case so the LLM always sees the position hints.
    preamble = "\n".join(preamble_lines)
    hints_match = re.search(
        r"^(##\s*(?:Hints|元素位置 Hints|元素位置参考)[^\n]*\n.*?)(?=\n##\s|\Z)",
        preamble, re.MULTILINE | re.DOTALL,
    )
    if hints_match:
        hints_block = hints_match.group(1).rstrip()
        cases = [f"{hints_block}\n\n{c}" for c in cases]

    return cases
