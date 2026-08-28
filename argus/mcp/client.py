"""argus MCP client — 让 argus 能调外部 MCP server（Figma MCP / Atlassian / 自部署 ...）。

**本模块为骨架**：提供连接 / 工具发现 / 调用的最小封装，并给 LLM 集成留好
转 OpenAI tool schema 的 adapter。当前 brain.py **还没** 接进来，
等到具体接入对象（如 Figma MCP 替换 figma_ops 硬编码 REST 调用）落实时再
按 README 里的 hook 点接入。

## 设计

- ``MCPClient`` — async API，管理一个 stdio MCP server 的生命周期
- ``MCPClientSync`` — 同步 facade，用 ``asyncio.run`` 包一层，适合 brain.py
  这种同步调用路径（每次 call 起一个 loop 开销 ~几 ms；高频场景请用 async）
- ``MCPRegistry`` — 加载多个 MCP server 的配置，统一发现 / 路由
- ``to_openai_tools(mcp_tools)`` — 把 MCP ToolDefinition 转成 OpenAI Chat
  Completions API 的 ``tools=[{type:"function", function:{...}}]`` 形态

## 配置文件 (.argus/mcp_clients.json)

```json
{
  "servers": {
    "figma": {
      "command": "npx",
      "args": ["@figma/mcp-server"],
      "env": {"FIGMA_TOKEN": "${FIGMA_TOKEN}"}
    }
  }
}
```

## 待接入

- brain.py: 在 prompt 系统消息里加 available MCP tools，把 LLM 的 tool_call
  routed 到 ``MCPRegistry.call_tool(server, name, args)``
- figma_ops.py: 用 figma MCP 替换 hardcoded REST 调用（前提是外部 figma MCP
  暴露 frames / export 等能力）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # argus 扩展字段（非 MCP 协议）：逻辑工具名 → server 实际工具名的映射，
    # 来自配置里的 ``argus_mapping``，供 figma_via_mcp 等调用方覆盖默认工具名
    tool_mapping: dict[str, str] = field(default_factory=dict)

    def to_stdio_params(self) -> StdioServerParameters:
        # ${VAR} 展开（仅简单形式，复杂逻辑让用户在配置外做）
        env = {k: _expand_env(v) for k, v in self.env.items()}
        return StdioServerParameters(
            command=self.command, args=list(self.args),
            env=env or None,
        )


_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(s: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), s)


def load_clients_config(path: str | Path | None = None) -> dict[str, ServerConfig]:
    """加载 MCP client 配置文件，返回 {server_name: ServerConfig}。

    缺省路径: ``./.argus/mcp_clients.json``。文件不存在时返回 {}。
    """
    if path is None:
        path = Path(".argus") / "mcp_clients.json"
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    out: dict[str, ServerConfig] = {}
    for name, raw in (data.get("servers") or {}).items():
        out[name] = ServerConfig(
            name=name,
            command=raw["command"],
            args=raw.get("args", []),
            env=raw.get("env", {}),
            tool_mapping=raw.get("argus_mapping", {}) or {},
        )
    return out


# ──────────────────────────────────────────────────────────────────
# Client (async)
# ──────────────────────────────────────────────────────────────────


class MCPClient:
    """单个 MCP server 的 async 连接 + 调用封装。

    用法:
        async with MCPClient(config) as cli:
            tools = await cli.list_tools()
            result = await cli.call_tool("foo", {"x": 1})
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self._session: ClientSession | None = None
        self._stdio_cm = None
        self._session_cm = None

    async def __aenter__(self) -> "MCPClient":
        params = self.config.to_stdio_params()
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        try:
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
        except BaseException:
            # __aenter__ 半路失败时外层 async with 不会调 __aexit__ —
            # 这里手动收尾已进入的 cm，否则 stdio spawn 的 server 子进程泄漏
            exc_info = sys.exc_info()
            if self._session is not None:
                with contextlib.suppress(Exception):
                    await self._session_cm.__aexit__(*exc_info)
            with contextlib.suppress(Exception):
                await self._stdio_cm.__aexit__(*exc_info)
            self._session = None
            self._session_cm = None
            self._stdio_cm = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(exc_type, exc, tb)
        self._session = None
        self._stdio_cm = None
        self._session_cm = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCPClient not entered (use 'async with')")
        return self._session

    async def list_tools(self) -> list[dict[str, Any]]:
        """返回该 server 暴露的所有 tool 元数据。

        每条结构 ``{name, description, inputSchema}``，inputSchema 是 JSON Schema。
        """
        result = await self.session.list_tools()
        tools_out: list[dict[str, Any]] = []
        for t in result.tools:
            tools_out.append({
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema,
            })
        return tools_out

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调一个 tool，返回结构化结果（文本 / image / resource 全部展开）。"""
        result = await self.session.call_tool(name, arguments or {})
        content_out: list[Any] = []
        for item in result.content:
            kind = getattr(item, "type", None)
            if kind == "text":
                content_out.append({"type": "text", "text": item.text})
            elif kind == "image":
                content_out.append({
                    "type": "image",
                    "mime_type": getattr(item, "mimeType", ""),
                    "data": item.data,
                })
            else:
                content_out.append({"type": kind, "raw": str(item)})
        return {
            "content": content_out,
            "is_error": bool(getattr(result, "isError", False)),
        }


# ──────────────────────────────────────────────────────────────────
# Sync facade (for brain.py / cli.py one-shot calls)
# ──────────────────────────────────────────────────────────────────


class MCPClientSync:
    """同步 facade — 每次调用起一个临时 asyncio loop 跑 MCP 通话。

    适合 one-shot 调用模式（启动 + list_tools + 几次 call_tool + 关闭）。
    高频路径请用 async ``MCPClient`` 复用同一连接，避免每次 spawn 子进程。
    """

    def __init__(self, config: ServerConfig):
        self.config = config

    def list_tools(self) -> list[dict[str, Any]]:
        async def _run():
            async with MCPClient(self.config) as cli:
                return await cli.list_tools()
        return asyncio.run(_run())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        async def _run():
            async with MCPClient(self.config) as cli:
                return await cli.call_tool(name, arguments)
        return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────
# Registry (multiple servers)
# ──────────────────────────────────────────────────────────────────


@dataclass
class MCPRegistry:
    """N 个 MCP server 配置的集合 — 让 brain.py 一次性遍历所有可用 tool。

    调用路径:
        reg = MCPRegistry.from_config()
        for server_name, tools in reg.list_all_tools_sync().items():
            ...
        result = reg.call_tool_sync("figma", "get_design_context", {...})
    """
    servers: dict[str, ServerConfig] = field(default_factory=dict)

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> "MCPRegistry":
        return cls(servers=load_clients_config(path))

    def list_all_tools_sync(self) -> dict[str, list[dict[str, Any]]]:
        """逐个 server 起短连接拿 tool 列表。出错的 server 用空 list 占位。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for name, cfg in self.servers.items():
            try:
                out[name] = MCPClientSync(cfg).list_tools()
            except Exception as e:
                out[name] = []
                # 不抛 — 让 brain 知道哪些 server 不可用即可
                from ..logger import get_logger
                get_logger("mcp.client").warning(
                    "MCP server %s 不可用: %s", name, e,
                )
        return out

    def call_tool_sync(self, server: str, name: str,
                       arguments: dict[str, Any] | None = None) -> Any:
        if server not in self.servers:
            raise KeyError(f"unknown MCP server: {server}")
        return MCPClientSync(self.servers[server]).call_tool(name, arguments)


# ──────────────────────────────────────────────────────────────────
# OpenAI tool schema adapter
# ──────────────────────────────────────────────────────────────────


def to_openai_tools(mcp_tools: list[dict[str, Any]],
                    server_prefix: str | None = None) -> list[dict[str, Any]]:
    """把 MCP tool 列表转 OpenAI Chat Completions 的 ``tools=[...]`` 格式。

    server_prefix 非空时，function name = ``<server_prefix>__<name>``，
    用于多 server 共存时反查（brain.py 收到 tool_call 后按 ``__`` 切回 server）。
    """
    out: list[dict[str, Any]] = []
    for t in mcp_tools:
        name = t["name"]
        if server_prefix:
            name = f"{server_prefix}__{name}"
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema") or {
                    "type": "object", "properties": {}, "additionalProperties": True,
                },
            },
        })
    return out


def split_prefixed_name(prefixed: str) -> tuple[str, str]:
    """把 ``server__tool`` 拆回 ``(server, tool)``。无前缀返回 ``("", name)``."""
    if "__" not in prefixed:
        return "", prefixed
    server, _, tool = prefixed.partition("__")
    return server, tool
