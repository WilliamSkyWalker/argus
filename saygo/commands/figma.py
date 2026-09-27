"""Figma design inspection, test generation and visual reviews."""

import json
from saygo.config import load_config
from pathlib import Path


def register(sub):
    # saygo figma <subcommand>
    figma_p = sub.add_parser("figma", help="Figma design integration")
    figma_sub = figma_p.add_subparsers(dest="figma_command", required=True)

    # saygo figma frames <url>
    frames_p = figma_sub.add_parser("frames", help="List frames in a Figma file")
    frames_p.add_argument("url", help="Figma file URL or file key")
    frames_p.add_argument("--page", default=None, help="Filter by page name")

    # saygo figma gen-tests <url>
    gen_p = figma_sub.add_parser("gen-tests", help="Generate test cases from Figma design")
    gen_p.add_argument("url", help="Figma URL (with optional node-id)")
    gen_p.add_argument("-o", "--output", default=None,
                       help="Save generated tests to file (.md/.yaml)")

    # saygo figma review <url>
    review_p = figma_sub.add_parser("review", help="Visual review: Figma vs actual screenshot")
    review_p.add_argument("url", help="Figma URL (with node-id for specific frame)")
    review_p.add_argument("--platform", choices=["ios", "android", "browser", "rdp"], default=None)
    review_p.add_argument("--screenshot", default=None,
                          help="Path to screenshot PNG (instead of live capture)")
    review_p.add_argument("-o", "--output", default=None,
                          help="Save review report to file (.json/.html)")
    figma_p.set_defaults(handler=cmd_figma)


def cmd_figma(args):
    """Handle figma subcommands."""
    cfg = load_config()
    figma_token = cfg["figma"]["token"]

    if not figma_token:
        print("Error: Figma token not configured.")
        print("Set FIGMA_TOKEN in .env (Figma → Settings → Personal Access Tokens)")
        return

    if args.figma_command == "frames":
        cmd_figma_frames(figma_token, args.url, page=args.page)
    elif args.figma_command == "gen-tests":
        cmd_figma_gen_tests(figma_token, args.url, cfg["llm"], output=args.output)
    elif args.figma_command == "review":
        cmd_figma_review(figma_token, args.url, cfg,
                         platform_name=args.platform,
                         screenshot_path=args.screenshot,
                         output=args.output)
    else:
        print("Usage: saygo figma {frames|gen-tests|review}")


def cmd_figma_frames(token: str, url: str, page: str | None = None):
    """List all frames in a Figma file."""
    from saygo.integrations.figma import parse_figma_url
    from saygo.integrations.figma_via_mcp import get_figma_client
    client = get_figma_client(token)
    file_key, _ = parse_figma_url(url) if "figma.com" in url else (url, None)

    frames = client.list_frames(file_key, page_name=page)
    if not frames:
        print("No frames found.")
        return

    print(f"{'Frame':<40s} {'Page':<20s} {'Size':<15s} {'ID'}")
    print("-" * 90)
    for f in frames:
        size = f"{f['width']:.0f}x{f['height']:.0f}"
        print(f"  {f['name']:<38s} {f['page']:<20s} {size:<15s} {f['id']}")


def cmd_figma_gen_tests(token: str, url: str, llm_config: dict,
                         output: str | None = None):
    """Generate test cases from Figma design."""
    from saygo.integrations.figma_ops import gen_tests_from_figma

    if not llm_config.get("api_key"):
        print("Error: LLM API key not configured. Set LLM_API_KEY in .env")
        return

    print("正在从 Figma 设计稿生成测试用例...\n")
    tests_yaml = gen_tests_from_figma(token, url, llm_config)

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(tests_yaml)
        print(f"\n测试用例已保存: {output}")
    else:
        print(tests_yaml)


def cmd_figma_review(token: str, url: str, cfg: dict,
                      platform_name: str | None = None,
                      screenshot_path: str | None = None,
                      output: str | None = None):
    """Visual review: compare Figma design with actual screenshot."""
    from saygo.integrations.figma_ops import visual_review, review_with_platform

    llm_config = cfg["llm"]
    if not llm_config.get("api_key"):
        print("Error: LLM API key not configured. Set LLM_API_KEY in .env")
        return

    if screenshot_path:
        # Use provided screenshot
        screenshot_png = Path(screenshot_path).read_bytes()
        print(f"使用截图: {screenshot_path}")
        result = visual_review(token, url, screenshot_png, llm_config)
    else:
        # Take live screenshot from platform
        pname = platform_name or cfg.get("platform", "ios")
        from saygo.platforms import create_platform
        platform = create_platform(pname, cfg)
        platform.setup(cfg)
        print(f"正在从 {pname} 平台截图...")
        result = review_with_platform(token, url, platform, llm_config)
        platform.teardown()

    # Display result
    score = result.get("score", 0)
    summary = result.get("summary", "")
    issues = result.get("issues", [])
    highlights = result.get("highlights", [])

    print(f"\n{'='*60}")
    print(f"视觉走查报告")
    print(f"{'='*60}")
    print(f"\n  还原度评分: {score}/100")
    print(f"  总结: {summary}\n")

    if issues:
        print("  问题列表:")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "?")
            cat = issue.get("category", "?")
            desc = issue.get("description", "")
            loc = issue.get("location", "")
            sev_icon = {"high": "!!!", "medium": " !!", "low": "  !"}.get(sev, "  ?")
            print(f"    {sev_icon} [{cat}] {desc}")
            if loc:
                print(f"          位置: {loc}")

    if highlights:
        print("\n  亮点:")
        for h in highlights:
            print(f"    + {h}")

    # Save report
    if output:
        report_data = {k: v for k, v in result.items() if k != "design_png"}
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        if output.endswith(".json"):
            Path(output).write_text(json.dumps(report_data, ensure_ascii=False, indent=2))
        else:
            # Simple HTML report for visual review
            _save_review_html(result, output if output.endswith(".html") else output + ".html")
        print(f"\n  报告已保存: {output}")


def _save_review_html(result: dict, path: str):
    """Save visual review result as HTML with embedded images."""
    import base64
    score = result.get("score", 0)
    summary = result.get("summary", "")
    issues = result.get("issues", [])
    highlights = result.get("highlights", [])

    design_png = result.get("design_png", b"")
    design_b64 = base64.standard_b64encode(design_png).decode() if design_png else ""

    score_color = "#22c55e" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"

    issues_html = ""
    for issue in issues:
        sev = issue.get("severity", "low")
        sev_cls = {"high": "sev-high", "medium": "sev-med"}.get(sev, "sev-low")
        issues_html += f"""
        <div class="issue {sev_cls}">
          <span class="sev">{sev.upper()}</span>
          <span class="cat">[{issue.get('category', '')}]</span>
          {issue.get('description', '')}
          <div class="loc">{issue.get('location', '')}</div>
        </div>"""

    highlights_html = "".join(f"<li>{h}</li>" for h in highlights)
    design_img = (f'<img src="data:image/png;base64,{design_b64}" class="design-img"/>'
                  if design_b64 else "")

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Saygo 视觉走查报告</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; padding: 24px; color: #333; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 20px; }}
  .score {{ font-size: 48px; font-weight: 700; color: {score_color}; }}
  .summary {{ font-size: 15px; color: #666; margin: 8px 0 16px; }}
  .issue {{ padding: 8px 12px; margin: 6px 0; background: #fff; border-radius: 6px;
            border-left: 4px solid #ccc; font-size: 14px; }}
  .sev {{ font-weight: 700; margin-right: 6px; }}
  .cat {{ color: #888; margin-right: 4px; }}
  .loc {{ font-size: 12px; color: #999; margin-top: 2px; }}
  .sev-high {{ border-left-color: #ef4444; }} .sev-high .sev {{ color: #ef4444; }}
  .sev-med {{ border-left-color: #f59e0b; }} .sev-med .sev {{ color: #f59e0b; }}
  .sev-low {{ border-left-color: #3b82f6; }} .sev-low .sev {{ color: #3b82f6; }}
  .design-img {{ max-width: 400px; border: 1px solid #ddd; border-radius: 8px; margin-top: 12px; }}
  ul {{ padding-left: 20px; }} li {{ margin: 4px 0; font-size: 14px; }}
  .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 24px; }}
</style></head><body>
  <h1>Saygo 视觉走查报告</h1>
  <div class="score">{score}</div>
  <div class="summary">{summary}</div>
  {f'<h2>设计稿</h2>{design_img}' if design_img else ''}
  <h2>问题 ({len(issues)})</h2>
  {issues_html if issues else '<p style="color:#999">无问题</p>'}
  {f'<h2>亮点</h2><ul>{highlights_html}</ul>' if highlights else ''}
  <div class="footer">Generated by Saygo</div>
</body></html>"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)
