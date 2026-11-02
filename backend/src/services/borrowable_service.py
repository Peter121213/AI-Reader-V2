"""可借鉴性设定拆解服务（通用机制，不依赖任何书籍特定知识）。

以 20 章为一个窗口滑动拆解全书，每个窗口产出三类"可借鉴"素材：
- cool_points: 爽点——本窗口值得学习的爽点及触发方式（升级 / 金手指 / 抽奖 / 打脸 / 收获 / 反转等）
- map_plots: 地图情节——借地图 / 藏宝图 / 地点转移推进剧情的桥段
- special_settings: 特殊设定——本书独有的世界观机制（区别于通用套路），标注首现章节

产出存入独立影子表 borrowable_analysis，可增量补跑。
"""

import json
import logging
from typing import Awaitable, Callable

from src.db import borrowable_store, chapter_store
from src.infra import config as _cfg
from src.infra.llm_client import get_llm_client
from src.services.story_arc_service import WINDOW_SIZE, HEAD_CHARS, TAIL_CHARS, window_ranges
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

_BORROW_SYSTEM_PROMPT = """你是资深网文编辑，负责把一段 20 章的情节窗口拆解为"可借鉴性设定"——供写作者学习这本书的爽点设计、地图情节与特殊设定。
输出必须是 JSON 对象，格式如下：
{
  "window_summary": "不超过150字的一句话总览（讲清这20章整体在干什么、爽感/看点如何推进）",
  "cool_points": [
    {"chapter": 5, "type": "爽点类型", "title": "不超过12字的短标题", "description": "这个爽点如何被触发：谁做了什么导致什么结果（不超过80字）"}
  ],
  "map_plots": [
    {"chapter": 9, "title": "不超过12字的短标题", "description": "如何借地图/藏宝图/地点转移推进剧情（不超过80字）"}
  ],
  "special_settings": [
    {"chapter": 2, "title": "设定名", "description": "该设定的规则、作用与首次出现位置说明（不超过80字）"}
  ]
}
要求：
1. cool_points 2-6 条：只挑真正有爽感的节点，type 从【升级突破/金手指/抽奖/打脸/收获/反转/装逼/打斗】中选最贴切的一个；description 写清"谁做了什么导致什么"，不要写泛泛的"实力变强了"。
2. map_plots 0-3 条：只写与地图、藏宝图、路线、地点转移强相关且推动剧情的情节；本窗口没有就返回空数组 []，不要硬凑。
3. special_settings 1-4 条：只提炼本书独有、有别于通用网文套路的机制或设定（例如自创的金手指规则、独特的修炼/收益体系）；通用的"修炼境界""门派"等不要写。
4. 控制篇幅：window_summary ≤150字，其余字段每条≤80字（title 更短）；宁可精简也不要截断。
5. 全部使用简体中文；角色名/设定名使用原文称呼；不要输出 JSON 以外的任何内容。"""

ProgressCb = Callable[[int, int], Awaitable[None]]


async def _build_window_context(novel_id: str, arc_start: int, arc_end: int) -> str:
    """Concatenate each chapter's head+tail excerpt into one prompt context."""
    parts: list[str] = []
    for ch in range(arc_start, arc_end + 1):
        row = await chapter_store.get_chapter_content(novel_id, ch)
        if not row:
            parts.append(f"第{ch}章（缺内容）")
            continue
        content = (row.get("content") or "").strip()
        title = row.get("title", "")
        if not content:
            parts.append(f"第{ch}章 {title}（空）")
            continue
        head = content[:HEAD_CHARS]
        tail = content[-TAIL_CHARS:] if len(content) > HEAD_CHARS + TAIL_CHARS else ""
        excerpt = head + (f"\n……（中略）……\n{tail}" if tail else "")
        parts.append(f"【第{ch}章 {title}】\n{excerpt}")
    return "\n\n".join(parts)


def _extract_borrow(content: str | dict) -> dict | None:
    """Normalize LLM output into {window_summary, cool_points, map_plots, special_settings}."""
    if isinstance(content, dict):
        data = content
    else:
        try:
            data = json.loads(str(content))
        except json.JSONDecodeError:
            return None

    summary = str(data.get("window_summary", "")).strip()

    def _clean_list(key: str) -> list[dict]:
        out: list[dict] = []
        for item in data.get(key) or []:
            if isinstance(item, dict) and any(str(v).strip() for v in item.values()):
                out.append({k: str(v).strip() for k, v in item.items() if str(v).strip()})
        return out

    cool = _clean_list("cool_points")
    maps = _clean_list("map_plots")
    settings = _clean_list("special_settings")

    if not summary and not cool and not maps and not settings:
        return None
    return {
        "window_summary": summary,
        "cool_points": cool[:6],
        "map_plots": maps[:3],
        "special_settings": settings[:4],
    }


async def analyze_window(novel_id: str, arc_start: int, arc_end: int) -> dict | None:
    """Run borrowable analysis on one 20-chapter window. Returns saved record or None."""
    context = await _build_window_context(novel_id, arc_start, arc_end)
    if not context.strip():
        return None

    client = get_llm_client()
    prompt = (
        f"请拆解第{arc_start}-{arc_end}章这个情节窗口（共 {arc_end - arc_start + 1} 章）的可借鉴性设定。\n"
        f"以下是每章的章首与章尾节选：\n\n{context}"
    )
    raw, usage = await client.generate(
        system=_BORROW_SYSTEM_PROMPT,
        prompt=prompt,
        format={"type": "json_object"},
        temperature=0.3,
        max_tokens=2000,
        timeout=180,
    )
    borrow = _extract_borrow(raw)
    if borrow is None:
        logger.warning(
            "borrowable: failed to parse LLM output for window %s-%s: %r",
            arc_start, arc_end, str(raw)[:200],
        )
        return None

    await borrowable_store.save_borrowable(novel_id, arc_start, arc_end, borrow, _cfg.LLM_MODEL)
    return {
        "arc_start": arc_start,
        "arc_end": arc_end,
        "borrow": borrow,
    }


async def analyze_all_windows(
    novel_id: str,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """Run borrowable analysis over all windows of the analyzed chapter range.

    Returns {"windows": [{arc_start, arc_end, ok}], "done", "failed"}.
    """
    analyzed_start, analyzed_end = await get_analyzed_range(novel_id)
    if analyzed_start == 0:
        return {"windows": [], "done": 0, "failed": []}
    ranges = window_ranges(novel_id, analyzed_start, analyzed_end)
    results: list[dict] = []
    failed: list[tuple[int, int]] = []
    done = 0
    for idx, (s, e) in enumerate(ranges):
        try:
            saved = await analyze_window(novel_id, s, e)
            ok = saved is not None
        except Exception as exc:
            logger.warning("borrowable: window %s-%s failed: %s", s, e, exc)
            ok = False
        results.append({"arc_start": s, "arc_end": e, "ok": ok})
        if ok:
            done += 1
        else:
            failed.append((s, e))
        if progress_cb is not None:
            await progress_cb(idx + 1, len(ranges))
    return {"windows": results, "done": done, "failed": failed}
