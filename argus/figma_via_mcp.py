"""Figma operations via MCP — 用外部 Figma MCP server 取代直接 REST 调用。

为什么有它：
- `figma.py` 是手撸的 REST 客户端，每加一种 Figma 能力要自己写 URL + 解析。
- 用户机器上往往已有 Figma MCP（``@figma/mcp-server`` 或类似），暴露了
  ``get_metadata`` / ``get_screenshot`` / ``get_design_context`` 等工具。
- 这里写一个跟 ``figma.FigmaClient`` 同形签名的薄壳，让 ``figma_ops.py``
  按需切换：MCP 可用走 MCP，不可用回落 REST。

工具名映射：
  默认假定 ``@figma/mcp-server`` 的名字（``get_metadata`` / ``get_screenshot``）。
  如果你的 MCP server 用了别名，在 ``.argus/mcp_clients.json`` 的对应 server
  下加 ``argus_mapping`` 字段覆盖：

      {"servers": {"figma": {
         "command": "...", "args": [...],
         "argus_mapping": {
            "metadata": "fetch_node",
            "screenshot": "render_png"
         }
      }}}

  ``argus_mapping`` 不在 MCP 协议里 — 是 argus 自己读的扩展字段。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .figma import FigmaNode, _parse_node
from .logger import get_logger

log = get_logger("figma_via_mcp")


DEFAULT_MAPPING = {
    "metadata": "get_metadata",
    "screenshot": "get_screenshot",
    "design_context": "get_design_context",
}


def _build_figma_url(file_key: str, node_id: str | None = None) -> str:
    """根据 file_key + node_id 重建一个 figma.com URL（MCP 工具通常吃 URL）。"""
    base = f"https://www.figma.com/design/{file_key}"
    if not node_id:
        return base
    # MCP / 通用 URL 形式：node-id=1-2（用横线代替冒号）
    safe_id = node_id.replace(":", "-")
    return f"{base}?node-id={safe_id}"


def _extract_text_payload(result: dict) -> str:
    """从 MCPClient.call_tool 返回里把所有 text 段合并成字符串。"""
    parts: list[str] = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


def _extract_image_payload(result: dict) -> bytes | None:
    """从 MCPClient.call_tool 返回里把 image base64 decode 成 bytes。"""
    for item in result.get("content", []):
        if item.get("type") != "image":
            continue
        data = item.get("data", "")
        try:
            return base64.b64decode(data)
        except Exception as e:
            log.warning("Figma MCP screenshot base64 decode 失败: %s", e)
            return None
    return None


@dataclass
class MCPFigmaClient:
    """Drop-in 替代 ``figma.FigmaClient``，方法签名一致。

    构造时传 MCPRegistry + server 名（默认 "figma"）。该 server 必须已经在
    ``.argus/mcp_clients.json`` 里注册（``argus.mcp.client.MCPRegistry``）。
    工具调用结果按 MCP 协议含 ``content: [{type, text|data}, ...]``，本类负
    责剥出 text/image 喂给上层。
    """

    registry: Any
    server_name: str = "figma"
    tool_mapping: dict[str, str] | None = None

    def __post_init__(self) -> None:
        mapping = dict(DEFAULT_MAPPING)
        # 允许从 ServerConfig.metadata 之类的扩展字段覆盖（当前 ServerConfig
        # 数据类不带 mapping 字段，预留接口给后续扩展）。
        if self.tool_mapping:
            mapping.update(self.tool_mapping)
        self.tool_mapping = mapping
        if self.server_name not in (self.registry.servers or {}):
            raise RuntimeError(
                f"MCP server '{self.server_name}' 未在 registry 注册 "
                f"(已注册: {list(self.registry.servers.keys())})"
            )

    def _call(self, logical: str, arguments: dict) -> dict:
        tool_name = self.tool_mapping[logical]
        log.info("Figma MCP 调用: %s(%s) → %s/%s",
                 logical, json.dumps(arguments, ensure_ascii=False)[:120],
                 self.server_name, tool_name)
        return self.registry.call_tool_sync(self.server_name, tool_name, arguments)

    # ── 与 FigmaClient 形状对齐的方法 ─────────────────────────────

    def get_file_pages(self, file_key: str) -> list[dict]:
        """取顶层 page 列表（document.children）。

        Figma MCP 的 ``get_metadata`` 通常返回 JSON 文本，结构与 Figma REST
        ``GET /files/{key}`` 同源。解析失败时返回空 list，让上层 fallback。
        """
        result = self._call("metadata", {
            "url": _build_figma_url(file_key),
            "depth": 2,
        })
        text = _extract_text_payload(result)
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Figma MCP get_metadata 返回非 JSON，无法解析")
            return []
        doc = data.get("document") or data
        return doc.get("children", [])

    def get_node(self, file_key: str, node_id: str) -> dict:
        """取单个节点的完整子树（JSON dict）。"""
        result = self._call("metadata", {
            "url": _build_figma_url(file_key, node_id),
        })
        text = _extract_text_payload(result)
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Figma MCP get_node 返回非 JSON")
            return {}
        # MCP 返回有两种常见形态：
        #   1. 直接是节点 dict
        #   2. { "nodes": { node_id: { "document": {...} } } } —— 跟 REST 一样
        if "nodes" in data and isinstance(data["nodes"], dict):
            entry = data["nodes"].get(node_id, {})
            return entry.get("document", entry)
        return data

    def export_png(self, file_key: str, node_id: str,
                   scale: float = 2.0) -> bytes:
        """导出节点 PNG。"""
        result = self._call("screenshot", {
            "url": _build_figma_url(file_key, node_id),
            "scale": scale,
        })
        png = _extract_image_payload(result)
        if png is None:
            raise RuntimeError(
                f"Figma MCP 没返回 image content for node {node_id}"
            )
        return png

    def export_frames(self, file_key: str, node_ids: list[str],
                      scale: float = 2.0) -> dict[str, bytes]:
        """批量导出 — 不少 MCP server 不暴露批量接口，串行调用 fallback。"""
        out: dict[str, bytes] = {}
        for nid in node_ids:
            try:
                out[nid] = self.export_png(file_key, nid, scale=scale)
            except Exception as e:
                log.warning("export_png(%s) 失败: %s", nid, e)
        return out

    def extract_structure(self, file_key: str, node_id: str) -> FigmaNode:
        """与 FigmaClient 同名：拉节点 JSON 后用 figma._parse_node 转 FigmaNode。"""
        raw = self.get_node(file_key, node_id)
        return _parse_node(raw)

    def list_frames(self, file_key: str,
                    page_name: str | None = None) -> list[dict]:
        """枚举 file 内所有顶层 FRAME / COMPONENT 节点。"""
        pages = self.get_file_pages(file_key)
        frames: list[dict] = []
        for page in pages:
            if page_name and page.get("name") != page_name:
                continue
            for child in page.get("children", []):
                if child.get("type") in ("FRAME", "COMPONENT", "COMPONENT_SET"):
                    bbox = child.get("absoluteBoundingBox", {})
                    frames.append({
                        "id": child["id"],
                        "name": child.get("name", ""),
                        "page": page.get("name", ""),
                        "width": bbox.get("width", 0),
                        "height": bbox.get("height", 0),
                    })
        return frames


# ──────────────────────────────────────────────────────────────────
# 高层 dispatcher — 给 figma_ops.py 用
# ──────────────────────────────────────────────────────────────────


def get_figma_client(token: str = "", server_name: str = "figma",
                     prefer_mcp: bool = True) -> Any:
    """返回一个 figma client：优先 MCP，没有时 fallback 到 REST。

    Args:
      token: Figma personal access token（REST fallback 用；MCP path 不需要）
      server_name: MCP registry 里的 figma server 名（默认 "figma"）
      prefer_mcp: True 时优先尝试 MCP；False 强制走 REST

    Returns:
      MCPFigmaClient 或 FigmaClient — 调用方按统一接口用即可
    """
    from .figma import FigmaClient
    from .mcp.client import MCPRegistry

    if prefer_mcp:
        try:
            registry = MCPRegistry.from_config()
            if server_name in (registry.servers or {}):
                log.info("Figma 走 MCP 路径 (server=%s)", server_name)
                return MCPFigmaClient(registry=registry, server_name=server_name)
        except Exception as e:
            log.warning("Figma MCP 初始化失败 (fallback to REST): %s", e)

    log.info("Figma 走 REST 路径 (FigmaClient)")
    return FigmaClient(token)
