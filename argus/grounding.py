"""Grounding 定位兜底 —— 用专用视觉定位模型精确定位元素坐标。

背景（借鉴 midscene 的多模型分工）：主 brain（如 gemini-2.5-flash 这类通用 VLM）
出的百分比坐标偶有偏差，点小按钮会空点。midscene 用 Qwen-VL / UI-TARS 这类**专门
做 grounding 训练**的模型直接输出元素坐标，精度更高。argus 把它接成「兜底」：只在
brain 的 tap 连续 no_effect（估坐标点空了）时，由 agent 在**代码层**换用 grounding
模型对同一目标重新精定位并直接重 tap —— 不再把控制权交回 brain 让它盲猜同一坐标
（见记忆 no-blind-retry）。

启用：配 LLM_MODEL_GROUNDING（空则本模块 disabled，locate() 恒返回 None）。
端点默认复用主 LLM 的 base_url / api_key，可用 LLM_GROUNDING_* 单独指定。

输入图 + 目标文字描述（brain 在 tap action 里带的 `target` 字段），输出目标中心的
设备像素坐标 (x, y)。协议与 brain 一致用**百分比**（问 % 比问绝对像素准）。
"""

from __future__ import annotations

import base64
import json
import re

from .logger import get_logger

log = get_logger("grounding")

_SYSTEM_PROMPT = (
    "你是一个精确的 UI 元素定位器。用户给你一张 App 截图和一个目标元素的文字描述，"
    "你要找到该元素在图中的**几何中心**，用百分比坐标表示。\n"
    "只返回 JSON：{\"found\": true/false, \"x_pct\": <0-100>, \"y_pct\": <0-100>}\n"
    "x_pct = 中心点在图片宽度的百分比(0=最左,100=最右)，y_pct = 在高度的百分比"
    "(0=顶,100=底)。坐标是**百分比 0-100**，不是像素、不是 0-1000。"
    "若图中找不到该元素，返回 {\"found\": false}。不要输出其他内容。"
)

# 从模型输出里抠坐标的兜底正则（UI-TARS 等 GUI 专用模型不吐 JSON，用自己的动作语法：
# click(point='x y') / start_box='(x,y)' / <point>x y</point> / (x,y) / [x, y]）。
_POINT_TAG_RE = re.compile(r"<point>\s*([\d.]+)[\s,]+([\d.]+)\s*</point>", re.I)
_BOX_RE = re.compile(r"(?:start_box|point|box)\s*=\s*'?\(?\s*([\d.]+)[\s,]+([\d.]+)", re.I)
_PAIR_RE = re.compile(r"[\(\[]\s*([\d.]+)\s*,\s*([\d.]+)\s*[\)\]]")

_TIMEOUT_S = 20


class GroundingLocator:
    """专用 grounding 模型定位器。model 为空 = disabled。"""

    def __init__(self, grounding_cfg: dict | None = None):
        cfg = grounding_cfg or {}
        self.model = (cfg.get("model") or "").strip()
        self._base_url = cfg.get("base_url") or ""
        self._api_key = cfg.get("api_key") or ""
        self._max_tokens = int(cfg.get("max_tokens") or 1024)
        self._headers = cfg.get("extra_headers") or None
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=_TIMEOUT_S,
                max_retries=0,
                default_headers=self._headers,
            )
        return self._client

    def locate(self, image_png: bytes, target_desc: str,
               screen_size: tuple[int, int]) -> tuple[int, int] | None:
        """定位 target_desc 描述的元素中心，返回设备像素 (x, y)；失败/未启用返回 None。"""
        if not self.enabled or not target_desc:
            return None
        w, h = screen_size
        if not (w and h):
            return None
        try:
            client = self._ensure_client()
            b64 = base64.standard_b64encode(image_png).decode()
            resp = client.chat.completions.create(
                model=self.model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"目标元素：{target_desc}"},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}",
                                       "detail": "high"}},
                    ]},
                ],
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            log.warning("grounding 模型调用失败: %s", e)
            return None

        xy = self._extract_xy(raw)
        if xy is None:
            log.info("grounding 未定位到「%s」(raw=%s)", target_desc, raw[:160])
            return None
        frac = self._to_fraction(xy[0], xy[1], w, h)
        if frac is None:
            log.info("grounding 坐标越界，弃用「%s」(raw=%s)", target_desc, raw[:160])
            return None
        fx, fy = frac
        x = max(0, min(int(round(fx * w)), w - 1))
        y = max(0, min(int(round(fy * h)), h - 1))
        log.info("grounding 定位「%s」→ %.1f%%,%.1f%% → px(%d,%d)",
                 target_desc, fx * 100, fy * 100, x, y)
        return (x, y)

    @staticmethod
    def _to_fraction(x: float, y: float, w: int, h: int) -> tuple[float, float] | None:
        """把模型给的坐标对归一到 0-1 分数。

        实测各家 grounding 模型无视「用 0-100」的指令，各用各的量纲：Qwen-VL 系用
        0-1000，UI-TARS 用绝对像素，也有守规矩给 0-100 的。故**不信指令，按量级推断**，
        且**整对用同一量纲**（取 max 判定，避免一轴百分比一轴千分比混判）：
          max≤1.5 → 已是 0-1 分数；≤100 → 0-100 百分比；≤1000 → 0-1000 千分比；
          否则 → 绝对像素（除以屏宽/高）。越界返回 None。
        """
        try:
            x = float(x); y = float(y)
        except (TypeError, ValueError):
            return None
        m = max(abs(x), abs(y))
        if m <= 1.5:
            fx, fy = x, y
        elif m <= 100:
            fx, fy = x / 100.0, y / 100.0
        elif m <= 1000:
            fx, fy = x / 1000.0, y / 1000.0
        else:
            fx = x / w if w else 0.0
            fy = y / h if h else 0.0
        if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            return (fx, fy)
        return None

    @classmethod
    def _extract_xy(cls, raw: str) -> tuple[float, float] | None:
        """从模型输出抠出一对原始坐标数（不做量纲归一，交给 _to_fraction）。

        兼容：指令模型的 JSON {x_pct,y_pct}/{x,y}（值可能是数，也可能是 [x,y] 数组）；
        GUI 专用模型（UI-TARS）的 click(point='x y') / <point>..</point> / (x,y) 语法。
        """
        text = (raw or "").strip()
        if not text:
            return None

        # ① JSON 路径（去 ```fence```）
        jtext = text
        if "```" in jtext:
            m = re.search(r"```(?:json)?\s*(.+?)\s*```", jtext, re.DOTALL)
            if m:
                jtext = m.group(1).strip()
        s, e = jtext.find("{"), jtext.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                data = json.loads(jtext[s:e + 1])
            except Exception:
                data = None
            if isinstance(data, dict):
                if data.get("found") is False:
                    return None
                # 值为 [x,y] 数组（实测 qwen 偶发 "x_pct":[944,38]）
                for k in ("x_pct", "x", "point", "center", "coordinate"):
                    v = data.get(k)
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        return (v[0], v[1])
                for kx, ky in (("x_pct", "y_pct"), ("x", "y")):
                    if kx in data and ky in data:
                        return (data[kx], data[ky])

        # ② 兜底：point 标签 / box= / 坐标对（UI-TARS 等）
        for rgx in (_POINT_TAG_RE, _BOX_RE, _PAIR_RE):
            m = rgx.search(text)
            if m:
                return (m.group(1), m.group(2))
        return None
