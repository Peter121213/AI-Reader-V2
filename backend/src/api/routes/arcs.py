"""大故事结构拆解 endpoints（20 章窗口：1类角色剧情作用 / 2类角色氛围铺垫 / 主角关键发展节点）。"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.db import arc_store, novel_store
from src.services import story_arc_service
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["arcs"])

# 运行中的分析任务（novel_id -> asyncio.Task），防重复触发
_running_tasks: dict[str, asyncio.Task] = {}


class ArcAnalyzeRequest(BaseModel):
    window_start: int | None = None
    window_end: int | None = None


@router.post("/novels/{novel_id}/arcs/analyze")
async def start_arc_analysis(
    novel_id: str,
    req: ArcAnalyzeRequest | None = None,
):
    """Trigger story-arc analysis over windows (default: all windows of analyzed range)."""
    novel = await novel_store.get_novel(novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="小说不存在")

    # 确定窗口范围：显式指定时从第1章起切窗口（不依赖全书预分析）；否则默认全部已分析章节的窗口
    novel_total = int(novel.get("total_chapters", 0) or 0)
    if req and req.window_start:
        w_start = max(1, int(req.window_start))
        w_end = int(req.window_end or w_start)
        if w_end < w_start:
            raise HTTPException(status_code=400, detail="窗口序号范围无效")
        total_windows = (novel_total + story_arc_service.WINDOW_SIZE - 1) // story_arc_service.WINDOW_SIZE if novel_total else 0
        if total_windows == 0:
            raise HTTPException(status_code=400, detail="该小说没有章节，无法拆解大故事结构")
        w_end = min(w_end, total_windows)
    else:
        analyzed_start, analyzed_end = await get_analyzed_range(novel_id)
        if analyzed_start == 0:
            raise HTTPException(status_code=400, detail="该小说尚未进行分析，无法拆解大故事结构")
        ranges = story_arc_service.window_ranges(novel_id, analyzed_start, analyzed_end)
        total_windows = len(ranges)
        w_start, w_end = 1, total_windows

    # 防重复触发
    existing = _running_tasks.get(novel_id)
    if existing and not existing.done():
        return {"started": True, "already_running": True,
                "window_start": w_start, "window_end": w_end, "total_windows": total_windows}

    async def _run():
        try:
            for idx in range(w_start, w_end + 1):
                s = (idx - 1) * story_arc_service.WINDOW_SIZE + 1
                e = min(s + story_arc_service.WINDOW_SIZE - 1, novel_total)
                try:
                    await story_arc_service.analyze_window(novel_id, s, e)
                except Exception:
                    logger.exception("arc window %s-%s failed", s, e)
        finally:
            _running_tasks.pop(novel_id, None)

    task = asyncio.create_task(_run())
    _running_tasks[novel_id] = task
    return {"started": True, "already_running": False,
            "window_start": w_start, "window_end": w_end, "total_windows": total_windows}


@router.get("/novels/{novel_id}/arcs")
async def get_arc_analysis(novel_id: str):
    """Get all window analyses for a novel, ordered by arc_start."""
    items = await arc_store.get_all_arcs(novel_id)
    return {"novel_id": novel_id, "items": items}


@router.get("/novels/{novel_id}/arcs/status")
async def get_arc_status(novel_id: str):
    """Return analyzed windows vs total windows."""
    novel = await novel_store.get_novel(novel_id)
    analyzed_start, analyzed_end = await get_analyzed_range(novel_id)
    total_windows = (
        len(story_arc_service.window_ranges(novel_id, analyzed_start, analyzed_end))
        if analyzed_start != 0 else 0
    )
    analyzed = await arc_store.count_arcs(novel_id)
    running = _running_tasks.get(novel_id)
    return {
        "novel_id": novel_id,
        "total_chapters": (novel or {}).get("total_chapters", 0),
        "total_windows": total_windows,
        "analyzed_windows": analyzed,
        "running": bool(running and not running.done()),
    }


@router.delete("/novels/{novel_id}/arcs")
async def clear_arc_analysis(novel_id: str):
    """Delete all window analyses for a novel."""
    deleted = await arc_store.delete_arcs(novel_id)
    return {"ok": True, "deleted": deleted}
