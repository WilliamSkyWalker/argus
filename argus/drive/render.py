"""把 argus-drive 的 journal.json 渲染成 HTML 报告。

journal 由 Claude 在跑测过程中用 Write 工具维护，结构如下：

    {
      "started_at": "2026-06-06T17:55:00",
      "feature": "tests/nb_cases/nb_mobile/02-feed/04-feed-foryou.feature",
      "scenarios": [
        {
          "case": "TC-FEED-001b｜Top Bar 元素齐全",
          "result": "pass" | "fail" | "skipped" | "timeout",
          "reason": "<short why for non-pass>",
          "duration": 12.3,
          "scenario_steps": ["Given ...", "When ...", "Then ..."],
          "step_status": {"1": "pass", "2": "pass", ...},
          "steps_detail": [
            {
              "step": 1,
              "screenshot": "/tmp/argus-drive/xxx.png",   # 文件路径，渲染时读字节
              "action": {"type": "tap", "x": 540, "y": 199},
              "observation": "...",
              "thinking": "..."
            }
          ]
        }
      ]
    }

只做两件事：
1. 把 step 里的 `screenshot` 字段（文件路径）替换成 `screenshot_png` 字节
2. 调 argus.report.save_html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..report import save_html


def _load_results(journal_path: Path) -> list[dict]:
    journal = json.loads(journal_path.read_text())
    out: list[dict] = []
    for sc in journal.get("scenarios", []):
        sc_out = dict(sc)
        steps_out = []
        for step in sc.get("steps_detail", []):
            step_out = dict(step)
            png_path = step_out.pop("screenshot", None)
            if png_path:
                p = Path(png_path)
                step_out["screenshot_png"] = p.read_bytes() if p.exists() else None
            else:
                step_out["screenshot_png"] = None
            steps_out.append(step_out)
        sc_out["steps_detail"] = steps_out
        out.append(sc_out)
    return out


def render(journal_path: str | Path, output_path: str | Path) -> str:
    results = _load_results(Path(journal_path))
    return save_html(results, str(output_path))


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m argus.drive.render")
    parser.add_argument("--journal", required=True,
                        help="argus-drive 跑测过程中累计的 journal.json 路径")
    parser.add_argument("--output", required=True,
                        help="HTML 报告输出路径")
    args = parser.parse_args()

    journal_path = Path(args.journal)
    if not journal_path.exists():
        print(f"journal not found: {journal_path}", file=sys.stderr)
        return 1

    out = render(journal_path, args.output)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
