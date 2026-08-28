"""元素定位（locator）—— 用专用小模型精确识别元素在屏上的坐标。

背景（借鉴 midscene 的多模型分工）：主 brain（如 gemini-2.5-flash 这类通用 VLM）
出的百分比坐标偶有偏差，点小按钮会空点。midscene 用 Qwen-VL / UI-TARS 这类**专门
做视觉定位（grounding）训练**的小模型直接输出元素坐标，精度更高。argus 把它接成
定位兜底：只在 brain 的 tap 连续 no_effect（估坐标点空了）时，由 agent 在**代码层**
换用定位小模型对同一目标重新精定位并直接重 tap —— 不再把控制权交回 brain 让它盲猜
同一坐标（见记忆 no-blind-retry）。

注：「grounding」在本项目指更大的定位兜底策略（后续还会有网格线版本、强模型版本）；
本模块只是它当前的一种实现——基于小模型的元素定位器（ElementLocator）。

启用：配 LLM_MODEL_LOCATOR（空则本模块 disabled，locate() 恒返回 None）。
端点默认复用主 LLM 的 base_url / api_key，可用 LLM_LOCATOR_* 单独指定。

输入图 + 目标文字描述（brain 在 tap action 里带的 `target` 字段），输出目标中心的
设备像素坐标 (x, y)。协议与 brain 一致用**百分比**（问 % 比问绝对像素准）。
"""

from __future__ import annotations

import base64
import json
import re

from .logger import get_logger

log = get_logger("locator")

_SYSTEM_PROMPT = (
    "你是一个精确的 UI 元素定位器。用户给你一张 App 截图和一个目标元素的文字描述，"
    "你要找到该元素在图中的**几何中心**，用百分比坐标表示。\n"
    "只返回 JSON：{\"found\": true/false, \"x_pct\": <0-100>, \"y_pct\": <0-100>}\n"
    "x_pct = 中心点在图片宽度的百分比(0=最左,100=最右)，y_pct = 在高度的百分比"
    "(0=顶,100=底)。坐标是**百分比 0-100**，不是像素、不是 0-1000。"
    "若图中找不到该元素，返回 {\"found\": false}。不要输出其他内容。"
)

# UI-TARS 系（GUI 专用定位模型）**必须**用它自己的原生 prompt——实测（见本模块 test 记录）：
# 喂上面的中文 JSON prompt，UI-TARS-1.5-7B 会被顶出训练分布，**同一模型同一 prompt 坐标量纲
# 时而 0-100 百分比、时而绝对像素**，无法在解析端可靠消歧；换回原生 `Action: click(...)` 语法
# 后输出**稳定为「所发送图片的绝对像素」**（借鉴 midscene：原生 prompt + 版本已知的确定性解析，
# 而非按量级猜）。故 UI-TARS 走独立 prompt + 独立「绝对像素」解析（见 _extract_xy_uitars /
# _px_from_uitars），与通用指令 VLM 的 JSON+量级启发式完全隔离，互不影响。
_UITARS_SYSTEM_PROMPT = (
    "You are a GUI agent. You are given a screenshot and a description of one "
    "target element. Output the action that clicks the **center** of that element.\n\n"
    "## Output Format\nAction: ...\n\n"
    "## Action Space\nclick(point='<point>x1 y1</point>')\n\n"
    "## Note\n"
    "- Coordinates are absolute pixels in the given image.\n"
    "- Output only the single Action line. No Thought, no extra text.\n\n"
    "## User Instruction\n"
)


def _is_uitars(model: str) -> bool:
    """model 名含 'tars'（如 bytedance/ui-tars-1.5-7b）→ 走 UI-TARS 原生定位链路。"""
    return "tars" in (model or "").lower()


# 从模型输出里抠坐标的兜底正则（UI-TARS 等 GUI 专用模型不吐 JSON，用自己的动作语法：
# click(point='x y') / start_box='(x,y)' / <point>x y</point> / (x,y) / [x, y]）。
_POINT_TAG_RE = re.compile(r"<point>\s*([\d.]+)[\s,]+([\d.]+)\s*</point>", re.I)
_BOX_RE = re.compile(r"(?:start_box|point|box)\s*=\s*'?\(?\s*([\d.]+)[\s,]+([\d.]+)", re.I)
_PAIR_RE = re.compile(r"[\(\[]\s*([\d.]+)\s*,\s*([\d.]+)\s*[\)\]]")

_TIMEOUT_S = 20


class ElementLocator:
    """专用小模型元素定位器。model 为空 = disabled。"""

    def __init__(self, locator_cfg: dict | None = None):
        cfg = locator_cfg or {}
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
        is_tars = _is_uitars(self.model)
        system = _UITARS_SYSTEM_PROMPT if is_tars else _SYSTEM_PROMPT
        user_text = target_desc if is_tars else f"目标元素：{target_desc}"
        try:
            client = self._ensure_client()
            b64 = base64.standard_b64encode(image_png).decode()
            resp = client.chat.completions.create(
                model=self.model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}",
                                       "detail": "high"}},
                    ]},
                ],
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            log.warning("定位模型调用失败: %s", e)
            return None

        # UI-TARS：原生动作语法 + 绝对像素解析（与通用 VLM 的 JSON+量级启发式隔离）。
        if is_tars:
            xy = self._extract_xy_uitars(raw)
            frac = self._px_from_uitars(xy[0], xy[1], w, h) if xy else None
        else:
            xy = self._extract_xy(raw)
            frac = self._to_fraction(xy[0], xy[1], w, h) if xy else None
        if xy is None:
            log.info("未定位到「%s」(raw=%s)", target_desc, raw[:160])
            return None
        if frac is None:
            log.info("定位坐标越界，弃用「%s」(raw=%s)", target_desc, raw[:160])
            return None
        fx, fy = frac
        x = max(0, min(int(round(fx * w)), w - 1))
        y = max(0, min(int(round(fy * h)), h - 1))
        log.info("元素定位「%s」→ %.1f%%,%.1f%% → px(%d,%d)",
                 target_desc, fx * 100, fy * 100, x, y)
        return (x, y)

    @staticmethod
    def _to_fraction(x: float, y: float, w: int, h: int) -> tuple[float, float] | None:
        """把模型给的坐标对归一到 0-1 分数。

        实测各家定位模型无视「用 0-100」的指令，各用各的量纲：Qwen-VL 系用
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

    @staticmethod
    def _extract_xy_uitars(raw: str) -> tuple[float, float] | None:
        """从 UI-TARS 原生动作输出抠坐标：`Action: click(point='(x y)')` /
        `click(start_box='(x,y)')` / `start_box='[x1,y1,x2,y2]'`（box 取中心，借鉴 midscene）。

        先剥掉 `Thought:` 段，取第一个 `click(...)` 括号内容里的数字；2 个=点，4 个=box→中心。
        """
        text = re.sub(r"Thought:.*?(?=Action:|$)", "", raw or "", flags=re.S)
        m = re.search(r"click\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)", text, re.I)
        seg = m.group(1) if m else text
        nums = re.findall(r"-?\d+\.?\d*", seg)
        if len(nums) >= 4:                       # box [x1,y1,x2,y2] → 中心
            x = (float(nums[0]) + float(nums[2])) / 2
            y = (float(nums[1]) + float(nums[3])) / 2
            return (x, y)
        if len(nums) >= 2:                       # point (x, y)
            return (float(nums[0]), float(nums[1]))
        return None

    @staticmethod
    def _px_from_uitars(x: float, y: float, w: int, h: int) -> tuple[float, float] | None:
        """UI-TARS 坐标归一：实测（native prompt）稳定输出**所发送图片的绝对像素**——
        直接除以屏宽/高。不走通用 _to_fraction 的量级启发式：那套把 (100,1000] 判成千分比，
        会把 UI-TARS 上半屏的绝对像素（如 (545,954)）误算到屏幕下方（→ (589,2290)）。
        仅当坐标越出图片范围时，才兜底按 0-1000 归一。越界返回 None。
        """
        try:
            x = float(x); y = float(y)
        except (TypeError, ValueError):
            return None
        if 0 <= x <= w and 0 <= y <= h:          # 绝对像素（观测到的稳定情形）
            fx, fy = x / w, y / h
        elif max(abs(x), abs(y)) <= 1000:        # 兜底：0-1000 归一
            fx, fy = x / 1000.0, y / 1000.0
        else:
            return None
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
