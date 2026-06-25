"""argus MCP integration.

- ``argus.mcp.server`` — 把 argus 能力（list / run / device）暴露成 MCP tools，
  供 Claude Code / Claude Desktop / Cursor 等 client 通过 stdio 调用。
- ``argus.mcp.client`` — 让 argus（brain.py 等）作为 MCP client 调外部服务的
  helper（如 Figma MCP / 自定义 UI tree server）。当前为骨架，等接入时落实业务。

启动 server：``python3 -m argus.mcp.server`` （stdio transport）
"""
