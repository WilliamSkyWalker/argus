"""argus-drive: 以 Claude Code 会话为主循环的人工驱动 driver。

由 .claude/skills/argus-drive/SKILL.md 编排：Claude 边跑边维护
journal.json，结尾调 argus.drive.render 复用 argus.report.save_html
生成与 argus.cli run 一致样式的 HTML 报告。
"""
