"""saygo MCP integration.

- ``saygo.mcp.server`` — 把 saygo 能力（list / run / device）暴露成 MCP tools，
  供 Claude Code / Claude Desktop / Cursor 等 client 通过 stdio 调用。
- ``saygo.mcp.client`` — 让 saygo（brain.py 等）作为 MCP client 调外部服务的
  helper（如 Figma MCP / 自定义 UI tree server）。当前为骨架，等接入时落实业务。

启动 server：``python3 -m saygo.mcp.server`` （stdio transport）
"""
