"""Figma integration — pull designs, extract structure, export images."""

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field

from .logger import get_logger

log = get_logger("figma")

FIGMA_API = "https://api.figma.com/v1"


@dataclass
class FigmaNode:
    """A simplified representation of a Figma node."""
    id: str
    name: str
    type: str  # FRAME, TEXT, RECTANGLE, COMPONENT, INSTANCE, GROUP, etc.
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    text: str = ""
    fill_color: str = ""
    font_size: float = 0
    visible: bool = True
    children: list["FigmaNode"] = field(default_factory=list)


class FigmaClient:
    """Lightweight Figma REST API client (no external dependencies)."""

    def __init__(self, token: str):
        if not token:
            raise ValueError(
                "Figma token not configured.\n"
                "1. Go to Figma → Settings → Personal Access Tokens\n"
                "2. Generate a token\n"
                "3. Set FIGMA_TOKEN in .env"
            )
        self._token = token

    def _get(self, url: str) -> dict:
        """Make an authenticated GET request to Figma API."""
        req = urllib.request.Request(url, headers={"X-Figma-Token": self._token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"Figma API error {e.code}: {body[:200]}") from e

    def _download(self, url: str) -> bytes:
        """Download raw bytes from a URL."""
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    # ── File & structure ──────────────────────────────────────

    def get_file(self, file_key: str) -> dict:
        """Get full file structure."""
        return self._get(f"{FIGMA_API}/files/{file_key}")

    def get_file_pages(self, file_key: str) -> list[dict]:
        """Get top-level pages (canvases) with their children."""
        data = self._get(f"{FIGMA_API}/files/{file_key}?depth=2")
        doc = data.get("document", {})
        return doc.get("children", [])

    def get_node(self, file_key: str, node_id: str) -> dict:
        """Get a specific node and its full subtree."""
        data = self._get(f"{FIGMA_API}/files/{file_key}/nodes?ids={node_id}")
        nodes = data.get("nodes", {})
        node_data = nodes.get(node_id, {})
        return node_data.get("document", {})

    # ── Image export ──────────────────────────────────────────

    def export_png(self, file_key: str, node_id: str, scale: float = 2.0) -> bytes:
        """Export a node (frame/component) as PNG bytes."""
        safe_id = node_id.replace(":", "%3A")
        url = (f"{FIGMA_API}/images/{file_key}"
               f"?ids={safe_id}&format=png&scale={scale}")
        data = self._get(url)
        images = data.get("images", {})
        img_url = images.get(node_id)
        if not img_url:
            raise RuntimeError(f"No image URL returned for node {node_id}")
        log.info("正在下载 Figma 设计图: %s", node_id)
        return self._download(img_url)

    def export_frames(self, file_key: str, node_ids: list[str],
                      scale: float = 2.0) -> dict[str, bytes]:
        """Export multiple frames as PNG. Returns {node_id: png_bytes}."""
        safe_ids = ",".join(nid.replace(":", "%3A") for nid in node_ids)
        url = (f"{FIGMA_API}/images/{file_key}"
               f"?ids={safe_ids}&format=png&scale={scale}")
        data = self._get(url)
        images = data.get("images", {})
        result = {}
        for nid in node_ids:
            img_url = images.get(nid)
            if img_url:
                result[nid] = self._download(img_url)
        return result

    # ── Structure extraction ──────────────────────────────────

    def extract_structure(self, file_key: str, node_id: str) -> FigmaNode:
        """Extract simplified UI structure from a Figma frame."""
        raw = self.get_node(file_key, node_id)
        return _parse_node(raw)

    def list_frames(self, file_key: str, page_name: str | None = None) -> list[dict]:
        """List all top-level frames in the file (or a specific page).

        Returns list of {id, name, page, width, height}.
        """
        pages = self.get_file_pages(file_key)
        frames = []
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


# ── Node parsing helpers ──────────────────────────────────────

def _parse_node(raw: dict) -> FigmaNode:
    """Recursively parse a Figma API node into FigmaNode."""
    bbox = raw.get("absoluteBoundingBox", {})
    node = FigmaNode(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        type=raw.get("type", ""),
        x=bbox.get("x", 0),
        y=bbox.get("y", 0),
        width=bbox.get("width", 0),
        height=bbox.get("height", 0),
        visible=raw.get("visible", True),
    )

    # Extract text content
    if raw.get("type") == "TEXT":
        node.text = raw.get("characters", "")
        style = raw.get("style", {})
        node.font_size = style.get("fontSize", 0)

    # Extract fill color
    fills = raw.get("fills", [])
    if fills and fills[0].get("type") == "SOLID":
        c = fills[0].get("color", {})
        r, g, b = int(c.get("r", 0) * 255), int(c.get("g", 0) * 255), int(c.get("b", 0) * 255)
        node.fill_color = f"#{r:02x}{g:02x}{b:02x}"

    # Recurse children
    for child_raw in raw.get("children", []):
        node.children.append(_parse_node(child_raw))

    return node


def node_to_summary(node: FigmaNode, indent: int = 0) -> str:
    """Convert a FigmaNode tree into a human-readable summary for LLM consumption."""
    lines = []
    prefix = "  " * indent
    desc_parts = [f"{node.type}: \"{node.name}\""]

    if node.text:
        desc_parts.append(f"text=\"{node.text}\"")
    if node.width and node.height:
        desc_parts.append(f"{node.width:.0f}x{node.height:.0f}")
    if node.fill_color:
        desc_parts.append(f"color={node.fill_color}")
    if node.font_size:
        desc_parts.append(f"fontSize={node.font_size:.0f}")

    lines.append(f"{prefix}- {', '.join(desc_parts)}")

    for child in node.children:
        if child.visible:
            lines.append(node_to_summary(child, indent + 1))

    return "\n".join(lines)


def parse_figma_url(url: str) -> tuple[str, str | None]:
    """Parse a Figma URL into (file_key, node_id).

    Supports:
      https://www.figma.com/file/XXXXX/Name
      https://www.figma.com/design/XXXXX/Name?node-id=1-2
      https://www.figma.com/proto/XXXXX/Name?node-id=1:2
    """
    # Extract file key
    m = re.search(r'figma\.com/(?:file|design|proto)/([a-zA-Z0-9]+)', url)
    if not m:
        raise ValueError(f"Invalid Figma URL: {url}")
    file_key = m.group(1)

    # Extract node-id if present
    node_id = None
    m2 = re.search(r'node-id=([0-9]+[-:][0-9]+)', url)
    if m2:
        node_id = m2.group(1).replace("-", ":")

    return file_key, node_id
