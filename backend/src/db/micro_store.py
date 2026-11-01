"""CRUD operations for chapter_micro_analysis table (细章拆解).

独立影子表：不碰主表 chapter_facts，可对已分析小说增量补跑。
"""

import json
import logging

from src.db.sqlite_db import get_connection

logger = logging.getLogger(__name__)


async def save_micro(
    novel_id: str,
    chapter_id: int,
    micro: dict,
    llm_model: str | None,
) -> None:
    """Insert or replace one chapter's micro-analysis."""
    conn = await get_connection()
    try:
        await conn.execute(
            """
            INSERT OR REPLACE INTO chapter_micro_analysis
                (novel_id, chapter_id, micro_json, llm_model)
            VALUES (?, ?, ?, ?)
            """,
            (novel_id, chapter_id, json.dumps(micro, ensure_ascii=False), llm_model),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_micro_by_chapter(
    novel_id: str,
    chapter_num: int,
) -> dict | None:
    """Get micro-analysis for one chapter (joined with chapters for num/title)."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT m.micro_json, m.llm_model, m.analyzed_at,
                   c.chapter_num, c.title
            FROM chapter_micro_analysis m
            JOIN chapters c ON c.id = m.chapter_id
            WHERE m.novel_id = ? AND c.chapter_num = ?
            """,
            (novel_id, chapter_num),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["micro"] = json.loads(data.pop("micro_json"))
        return data
    finally:
        await conn.close()


async def get_all_micros(novel_id: str) -> list[dict]:
    """Get all micro-analyses for a novel, ordered by chapter_num."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            """
            SELECT m.micro_json, m.llm_model, m.analyzed_at,
                   c.chapter_num, c.title
            FROM chapter_micro_analysis m
            JOIN chapters c ON c.id = m.chapter_id
            WHERE m.novel_id = ?
            ORDER BY c.chapter_num ASC
            """,
            (novel_id,),
        )
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["micro"] = json.loads(data.pop("micro_json"))
            out.append(data)
        return out
    finally:
        await conn.close()


async def count_micros(novel_id: str) -> int:
    """Count analyzed chapters for a novel."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM chapter_micro_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await conn.close()


async def delete_micros(novel_id: str) -> int:
    """Delete all micro-analyses for a novel. Returns rows deleted."""
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "DELETE FROM chapter_micro_analysis WHERE novel_id = ?",
            (novel_id,),
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()
