"""saygo-drive: 以 Claude Code 会话为主循环的人工驱动 driver。

由 .claude/skills/saygo-drive/SKILL.md 编排：Claude 边跑边维护
journal.json，结尾调 saygo.drive.render 复用 saygo.qa.report.save_html
生成与 saygo.cli run 一致样式的 HTML 报告。
"""
