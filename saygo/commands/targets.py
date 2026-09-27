"""Scaffold and list test targets."""

import json
import re
import shutil
from saygo.config import PROJECT_ROOT
from saygo.qa.cases import TESTS_DIR


def register(sub):
    # saygo new <target>
    new_p = sub.add_parser("new", help="Scaffold a new test target under tests/")
    new_p.add_argument("name", help="Target name (becomes tests/<name>/)")
    new_p.add_argument("--platform", choices=["ios", "android", "browser", "rdp"],
                       required=True, help="Target platform (rdp is experimental)")
    new_p.add_argument("--package", default=None, metavar="PKG",
                       help="Android package name (e.g. com.example.app); android only")
    new_p.add_argument("--url", default=None, metavar="URL",
                       help="Site URL for browser targets (written into README)")
    new_p.add_argument("--force", action="store_true",
                       help="Overwrite if tests/<name>/ already exists")

    # saygo list
    list_p = sub.add_parser("list", help="List available test targets")
    list_p.add_argument("--json", action="store_true", help="Emit JSON to stdout (machine-readable)")
    new_p.set_defaults(handler=dispatch)
    list_p.set_defaults(handler=dispatch)


def dispatch(args):
    if args.command == "new":
        cmd_new(args.name, args.platform, package=args.package, url=args.url, force=args.force)
    else:
        cmd_list_targets(args.json)


def cmd_new(name: str, platform: str, package: str | None = None,
            url: str | None = None, force: bool = False):
    """Scaffold tests/<name>/ from tests/_template/, filling the .feature
    metadata header for the chosen platform."""
    template_dir = TESTS_DIR / "_template"
    target = TESTS_DIR / name
    if not template_dir.exists():
        print(f"模板缺失：{template_dir} 不存在，无法新建。")
        return
    if target.exists():
        if not force:
            print(f"目标已存在：{target}（要覆盖请加 --force）")
            return
        shutil.rmtree(target)
    if platform == "android" and not package:
        print("⚠️ android target 没给 --package，先填占位 com.example.app；"
              "记得改 .feature 头并在跑测时配 ANDROID_PACKAGE。")

    shutil.copytree(template_dir, target)
    # 旧 .md 样例不应再带入新 target（统一 .feature）
    for stale in (target / "cases").glob("*.md"):
        stale.unlink()

    # 定制 example.feature 的元数据头
    feat = target / "cases" / "example.feature"
    if feat.exists():
        out = []
        for ln in feat.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("# saygo-target:"):
                out.append(f"# saygo-target: {name}")
            elif s.startswith("# saygo-platform:"):
                out.append(f"# saygo-platform: {platform}")
            elif s.startswith("# saygo-package:"):
                if platform == "android":
                    out.append(f"# saygo-package: {package or 'com.example.app'}")
                # 非 android 丢掉 package 行
            elif s.startswith("# saygo-reset-default:"):
                if platform == "android":
                    out.append(ln)
                # reset-default 仅 android 有意义，非 android 丢掉
            elif s.startswith("@") and "@android" in s:
                # scenario 平台 tag：android 保留；ios/rdp 换标签；browser 不加平台标签
                # （平台标签是可扩展集合；模板当前以 @android 为起点）
                if platform == "ios":
                    out.append(ln.replace("@android", "@ios"))
                elif platform == "rdp":
                    out.append(ln.replace("@android", "@rdp"))
                elif platform == "browser":
                    out.append(re.sub(r"\s*@android\b", "", ln))
                else:
                    out.append(ln)
            else:
                out.append(ln)
        feat.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 轻量定制 README
    readme = target / "README.md"
    if readme.exists():
        txt = readme.read_text(encoding="utf-8").replace("# [项目名称]", f"# {name}", 1)
        if url:
            txt = txt.replace("https://example.com", url)
        if platform == "android" and package:
            txt = txt.replace("com.example.app", package)
        readme.write_text(txt, encoding="utf-8")

    print(f"✓ 已创建 target: {target.relative_to(PROJECT_ROOT)}  (platform={platform}"
          + (f", package={package}" if platform == "android" and package else "") + ")")
    for p in sorted(target.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(TESTS_DIR)}")
    print("\n下一步：")
    print(f"  1. 改 tests/{name}/cases/example.feature 写真实用例（自包含；值行别写行内 # 注释）")
    print(f"  2. 改 tests/{name}/_preconditions.md 为你产品真实的状态恢复 + 首页→子页导航")
    print(f"  3. 多账号并发：cp tests/{name}/_accounts.json.example tests/{name}/_accounts.json 填真账号（别 commit）")
    print(f"  跑：saygo run {name}")


def cmd_list_targets(as_json: bool = False):
    """List available test targets under tests/."""
    if not TESTS_DIR.exists():
        print(json.dumps([]) if as_json else "没有测试目录。请创建 tests/ 目录。")
        return

    targets = sorted(
        d for d in TESTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "cases").is_dir()
    )
    rows = []
    for t in targets:
        cases = list((t / "cases").rglob("*.feature")) + list((t / "cases").rglob("*.md"))
        reports_dir = t / "reports"
        reports = list(reports_dir.glob("*.html")) if reports_dir.exists() else []
        readme = t / "README.md"
        desc = ""
        if readme.exists():
            for line in readme.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("["):
                    desc = line[:30]
                    break
        rows.append({"target": t.name, "cases": len(cases),
                     "reports": len(reports), "desc": desc})

    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print("没有可用的测试目标。")
        return
    print(f"{'目标':<20s} {'用例数':<8s} {'报告数':<8s} 说明")
    print("-" * 65)
    for r in rows:
        print(f"  {r['target']:<18s} {r['cases']:<8d} {r['reports']:<8d} {r['desc']}")
