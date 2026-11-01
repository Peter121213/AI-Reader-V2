"""CRUD operations for story_arc_analysis table (大故事结构拆解).

独立影子表：不碰主表 chapter_facts，可对已分析小说增量补跑。
每行 = 一个 20 章窗口的大故事结构（1类角色剧情作用 / 2类角色氛围铺垫 / 主角关键发展节点）。
"""

import json
import logging

from src.db.sqlite_db import get_connection

logger = logging.getLogger(__name__)


async def save_arc(
    novel_id: str,
    arc_start: int,
    arc_end: int,
    arc: dict,
    llm_model: str | None,
) -> None:
    """Insert or replace one 20-chapter window's story-arc analysis."""
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT OR REPLACE INTO story_arc_analysis
                (novel_id, arc_start, arc_end, arc_json, llm_model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (novel_id, arc_start, arc_end, json.dumps(arc, ensure_ascii=False), llm_model),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_arc(novel_id: str, arc_start: int, arc_end: int) -> dict | None:
    """Get one window's analysis."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT arc_json, llm_model, analyzed_at, arc_start, arc_end
            FROM story_arc_analysis
            WHERE novel_id = ? AND arc_start = ? AND arc_end = ?
            """,
            (novel_id, arc_start, arc_end),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["arc"] = json.loads(data.pop("arc_json"))
        return data
    finally:
        await conn.close()


async def get_all_arcs(novel_id: str) -> list[dict]:
    """Get all window analyses for a novel, ordered by arc_start."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT arc_json, llm_model, analyzed_at, arc_start, arc_end
            FROM story_arc_analysis
            WHERE novel_id = ?
            ORDER BY arc_start ASC
            """,
            (novel_id,),
        )
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["arc"] = json.loads(data.pop("arc_json"))
            out.append(data)
        return out
    finally:
        await conn.close()


async def count_arcs(novel_id: str) -> int:
    """Count analyzed windows for a novel."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM story_arc_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await conn.close()


async def delete_arcs(novel_id: str) -> int:
    """Delete all window analyses for a novel. Returns rows deleted."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "DELETE FROM story_arc_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()
