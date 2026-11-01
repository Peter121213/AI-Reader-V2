"""细章拆解分析服务（通用机制，不依赖任何书籍特定知识）。

对单个章节调用 LLM，产出该章的结构化小结：
- summary: 不超过 100 字的一句话总览
- points: 3-6 条结构要点（引入冲突/升级/反转/伏笔/收束等）

产出存入独立影子表 chapter_micro_analysis，可对已分析小说增量补跑。
"""

import json
import logging
from typing import Awaitable, Callable

from src.db import chapter_store, micro_store
from src.infra import config as _cfg
from src.infra.llm_client import get_llm_client
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

_MICRO_SYSTEM_PROMPT = """你是资深网文编辑，负责把单个章节拆解为"可借鉴的细章结构小结"。
输出必须是 JSON 对象，格式如下：
{
  "summary": "不超过100字的一句话总览（讲清本章在全书中的功能定位）",
  "points": ["要点1", "要点2", ...]
}
要求：
1. points 为 3-6 条，每条一句话，聚焦本章结构动作：冲突引入、矛盾升级、反转、伏笔埋设、情绪渲染、高潮爆发、收束/钩子等。
2. 每条要点要具体到本章情节（写清"谁做了什么导致什么"），不要写泛泛的"主角成长"。
3. 全部使用简体中文，不要输出 JSON 以外的任何内容。"""

ProgressCb = Callable[[int, int], Awaitable[None]]


def _extract_micro(content: str | dict) -> dict | None:
    """Normalize LLM output into {summary, points}."""
    if isinstance(content, dict):
        data = content
    else:
        try:
            data = json.loads(str(content))
        except json.JSONDecodeError:
            return None
    summary = str(data.get("summary", "")).strip()
    points_raw = data.get("points") or []
    points = [str(p).strip() for p in points_raw if str(p).strip()]
    if not summary and not points:
        return None
    return {"summary": summary, "points": points[:6]}


async def analyze_chapter(novel_id: str, chapter_num: int) -> dict | None:
    """Run micro-analysis on a single chapter. Returns saved record or None."""
    row = await chapter_store.get_chapter_content(novel_id, chapter_num)
    if not row:
        logger.warning("micro: chapter %s/%s not found", novel_id, chapter_num)
        return None
    content = (row.get("content") or "").strip()
    if not content:
        return None
    title = row.get("title", "")

    client = get_llm_client()
    prompt = (
        f"小说章节：第{chapter_num}章 {title}\n"
        f"章节原文（约{len(content)}字）：\n{content}"
    )
    raw, usage = await client.generate(
        system=_MICRO_SYSTEM_PROMPT,
        prompt=prompt,
        format={"type": "json_object"},
        temperature=0.3,
        max_tokens=500,
        timeout=120,
    )
    micro = _extract_micro(raw)
    if micro is None:
        logger.warning("micro: failed to parse LLM output for ch%s: %r", chapter_num, str(raw)[:200])
        return None

    await micro_store.save_micro(novel_id, row["id"], micro, _cfg.LLM_MODEL)
    return {
        "chapter_num": chapter_num,
        "title": title,
        "micro": micro,
    }


async def analyze_range(
    novel_id: str,
    chapter_start: int,
    chapter_end: int,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """Run micro-analysis over a chapter range, tolerating per-chapter failure.

    Returns {"total", "done", "failed", "failed_chapters"}.
    """
    total = 0
    done = 0
    failed_chapters: list[int] = []
    for ch in range(chapter_start, chapter_end + 1):
        total += 1
        try:
            saved = await analyze_chapter(novel_id, ch)
            if saved:
                done += 1
            else:
                failed_chapters.append(ch)
        except Exception as exc:
            logger.warning("micro: chapter %s failed: %s", ch, exc)
            failed_chapters.append(ch)
        if progress_cb is not None:
            await progress_cb(ch, chapter_end)
    return {
        "total": total,
        "done": done,
        "failed": failed_chapters,
    }
