"""伏笔回收拆解服务（通用机制，不依赖任何书籍特定知识）。

以 20 章为一个窗口滑动拆解全书，每个窗口产出伏笔清单：
- 本窗口内新埋设的伏笔（埋设章 / 内容 / 是否已在本窗口回收）
- 本窗口内回收/呼应的伏笔（回收章 / 对应埋设章 / 关联说明）

产出存入独立影子表 foreshadow_analysis，可增量补跑。
"""

import json
import logging
from typing import Awaitable, Callable

from src.db import foreshadow_store, chapter_store
from src.infra import config as _cfg
from src.infra.llm_client import get_llm_client
from src.services.story_arc_service import WINDOW_SIZE, HEAD_CHARS, TAIL_CHARS, window_ranges
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

_FORESHADOW_SYSTEM_PROMPT = """你是资深网文编辑，负责从一段 20 章的情节窗口中识别"伏笔的埋设与回收"——帮助作者检查线索有没有埋下、有没有回收。
输出必须是 JSON 对象，格式如下：
{
  "window_summary": "不超过150字的一句话总览（本窗口埋设了哪些伏笔、回收/呼应了哪些）",
  "foreshadows": [
    {
      "plant_chapter": 3,
      "recall_chapter": 8,
      "content": "伏笔内容（不超过60字）",
      "status": "已回收",
      "note": "关联说明（不超过50字）"
    }
  ]
}
字段说明：
- plant_chapter：伏笔在本窗口内的埋设章号（必须是 1-20 内的具体章节，从原文可指认）；若该伏笔埋设于本窗口之前（前文已埋、本章回收），填 0，并在 note 里写"埋设于前文窗口"。
- recall_chapter：本窗口内明确回收/呼应的章号；尚未回收的填 null。
- status：与 recall_chapter 对应——有回收章填"已回收"，无则填"待回收"。
- note：说明这条伏笔怎么呼应（例如"与第5章的玉坠呼应""埋设于前文窗口，此处揭开身世"）；没有可写""。
要求：
1. foreshadows 1-6 条：只收真正的伏笔——人物身世疑点、特殊物件去向、预言/签文/梦兆、未解之局、计划伏线、台词铺垫等；普通情节推进、日常对话不要当作伏笔。
2. 埋设与回收必须能从窗口正文中指认章节；不要凭空推断"大概埋在第几章"。
3. 同一伏笔在窗口内完成"埋→收"的，作为一条记录（plant_chapter 与 recall_chapter 都填）；只埋未收的，recall_chapter 填 null；前文所埋、本窗口回收的，plant_chapter 填 0。
4. 控制篇幅：window_summary ≤150字，content ≤60字，note ≤50字；宁可精简也不要截断。
5. 全部使用简体中文；角色名/物件名使用原文称呼；不要输出 JSON 以外的任何内容。"""

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


def _to_int(value) -> int | None:
    try:
        v = int(value)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _extract_foreshadow(content: str | dict) -> dict | None:
    """Normalize LLM output into {window_summary, foreshadows}."""
    if isinstance(content, dict):
        data = content
    else:
        try:
            data = json.loads(str(content))
        except json.JSONDecodeError:
            return None

    summary = str(data.get("window_summary", "")).strip()

    items: list[dict] = []
    for item in data.get("foreshadows") or []:
        if not isinstance(item, dict):
            continue
        content_str = str(item.get("content", "")).strip()
        if not content_str:
            continue
        plant = _to_int(item.get("plant_chapter"))
        recall = _to_int(item.get("recall_chapter"))
        if plant is None:
            continue
        status = str(item.get("status", "")).strip()
        if status not in ("已回收", "待回收"):
            status = "已回收" if recall is not None else "待回收"
        items.append({
            "plant_chapter": plant,
            "recall_chapter": recall,
            "content": content_str[:80],
            "status": status,
            "note": str(item.get("note", "")).strip()[:60],
        })

    if not summary and not items:
        return None
    return {
        "window_summary": summary,
        "foreshadows": items[:8],
    }


async def analyze_window(novel_id: str, arc_start: int, arc_end: int) -> dict | None:
    """Run foreshadow analysis on one 20-chapter window. Returns saved record or None."""
    context = await _build_window_context(novel_id, arc_start, arc_end)
    if not context.strip():
        return None

    client = get_llm_client()
    prompt = (
        f"请识别第{arc_start}-{arc_end}章这个情节窗口（共 {arc_end - arc_start + 1} 章）中的伏笔埋设与回收。\n"
        f"以下是每章的章首与章尾节选：\n\n{context}"
    )
    raw, usage = await client.generate(
        system=_FORESHADOW_SYSTEM_PROMPT,
        prompt=prompt,
        format={"type": "json_object"},
        temperature=0.3,
        max_tokens=2000,
        timeout=180,
    )
    foreshadow = _extract_foreshadow(raw)
    if foreshadow is None:
        logger.warning(
            "foreshadow: failed to parse LLM output for window %s-%s: %r",
            arc_start, arc_end, str(raw)[:200],
        )
        return None

    await foreshadow_store.save_foreshadow(novel_id, arc_start, arc_end, foreshadow, _cfg.LLM_MODEL)
    return {
        "arc_start": arc_start,
        "arc_end": arc_end,
        "foreshadow": foreshadow,
    }


async def analyze_all_windows(
    novel_id: str,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """Run foreshadow analysis over all windows of the analyzed chapter range.

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
            logger.warning("foreshadow: window %s-%s failed: %s", s, e, exc)
            ok = False
        results.append({"arc_start": s, "arc_end": e, "ok": ok})
        if ok:
            done += 1
        else:
            failed.append((s, e))
        if progress_cb is not None:
            await progress_cb(idx + 1, len(ranges))
    return {"windows": results, "done": done, "failed": failed}
