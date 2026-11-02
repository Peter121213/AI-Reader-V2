"""CRUD operations for borrowable_analysis table (可借鉴性设定拆解).

独立影子表：不碰主表 chapter_facts，可对已分析小说增量补跑。
每行 = 一个 20 章窗口的可借鉴性设定（爽点 / 地图情节 / 特殊设定）。
"""

import json
import logging

from src.db.sqlite_db import get_connection

logger = logging.getLogger(__name__)


async def save_borrowable(
    novel_id: str,
    arc_start: int,
    arc_end: int,
    borrow: dict,
    llm_model: str | None,
) -> None:
    """Insert or replace one 20-chapter window's borrowable analysis."""
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT OR REPLACE INTO borrowable_analysis
                (novel_id, arc_start, arc_end, borrow_json, llm_model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (novel_id, arc_start, arc_end, json.dumps(borrow, ensure_ascii=False), llm_model),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_borrowable(novel_id: str, arc_start: int, arc_end: int) -> dict | None:
    """Get one window's analysis."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT borrow_json, llm_model, analyzed_at, arc_start, arc_end
            FROM borrowable_analysis
            WHERE novel_id = ? AND arc_start = ? AND arc_end = ?
            """,
            (novel_id, arc_start, arc_end),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["borrow"] = json.loads(data.pop("borrow_json"))
        return data
    finally:
        await conn.close()


async def get_all_borrowables(novel_id: str) -> list[dict]:
    """Get all window analyses for a novel, ordered by arc_start."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT borrow_json, llm_model, analyzed_at, arc_start, arc_end
            FROM borrowable_analysis
            WHERE novel_id = ?
            ORDER BY arc_start ASC
            """,
            (novel_id,),
        )
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["borrow"] = json.loads(data.pop("borrow_json"))
            out.append(data)
        return out
    finally:
        await conn.close()


async def count_borrowables(novel_id: str) -> int:
    """Count analyzed windows for a novel."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM borrowable_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await conn.close()


async def delete_borrowables(novel_id: str) -> int:
    """Delete all window analyses for a novel. Returns rows deleted."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "DELETE FROM borrowable_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()
