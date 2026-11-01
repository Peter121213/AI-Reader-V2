"""细章拆解 endpoints（每章 ≤100 字、分小点结构拆解）。"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.db import chapter_store, micro_store, novel_store
from src.services import micro_chapter_service
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["micro"])

# 运行中的分析任务（novel_id -> asyncio.Task），防重复触发
_running_tasks: dict[str, asyncio.Task] = {}


class MicroAnalyzeRequest(BaseModel):
    chapter_start: int | None = None
    chapter_end: int | None = None


@router.post("/novels/{novel_id}/micro/analyze")
async def start_micro_analysis(
    novel_id: str,
    req: MicroAnalyzeRequest | None = None,
):
    """Trigger micro-analysis over a chapter range (default: all analyzed chapters)."""
    novel = await novel_store.get_novel(novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="小说不存在")

    # 确定分析范围：显式指定时用指定范围（不依赖全书预分析）；否则默认全部已分析章节
    novel_total = int(novel.get("total_chapters", 0) or 0)
    if req and req.chapter_start:
        ch_start = max(1, int(req.chapter_start))
        ch_end = min(novel_total, int(req.chapter_end or ch_start))
        if novel_total == 0 or ch_start > ch_end:
            raise HTTPException(status_code=400, detail="章节范围无效")
    else:
        analyzed_start, analyzed_end = await get_analyzed_range(novel_id)
        if analyzed_start == 0:
            raise HTTPException(status_code=400, detail="该小说尚未进行分析，无法拆解细章")
        ch_start, ch_end = analyzed_start, analyzed_end

    # 防重复触发
    existing = _running_tasks.get(novel_id)
    if existing and not existing.done():
        return {"started": True, "already_running": True,
                "chapter_start": ch_start, "chapter_end": ch_end}

    async def _run():
        try:
            await micro_chapter_service.analyze_range(novel_id, ch_start, ch_end)
        except Exception:
            logger.exception("micro analysis task failed for %s", novel_id)
        finally:
            _running_tasks.pop(novel_id, None)

    task = asyncio.create_task(_run())
    _running_tasks[novel_id] = task
    return {"started": True, "already_running": False,
            "chapter_start": ch_start, "chapter_end": ch_end}


@router.get("/novels/{novel_id}/micro")
async def get_micro_analysis(novel_id: str):
    """Get all micro-analyses for a novel, ordered by chapter_num."""
    items = await micro_store.get_all_micros(novel_id)
    return {"novel_id": novel_id, "items": items}


@router.get("/novels/{novel_id}/micro/status")
async def get_micro_status(novel_id: str):
    """Return analyzed count vs total chapters."""
    novel = await novel_store.get_novel(novel_id)
    total = (novel or {}).get("total_chapters", 0)
    analyzed = await micro_store.count_micros(novel_id)
    running = _running_tasks.get(novel_id)
    return {
        "novel_id": novel_id,
        "total_chapters": total,
        "analyzed_chapters": analyzed,
        "running": bool(running and not running.done()),
    }


@router.delete("/novels/{novel_id}/micro")
async def clear_micro_analysis(novel_id: str):
    """Delete all micro-analyses for a novel."""
    deleted = await micro_store.delete_micros(novel_id)
    return {"ok": True, "deleted": deleted}
