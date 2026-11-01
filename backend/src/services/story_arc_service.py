"""大故事结构拆解服务（通用机制，不依赖任何书籍特定知识）。

以 20 章为一个窗口滑动拆解全书，每个窗口产出：
- window_summary: 该窗口总体叙事弧（≤150 字）
- primary_roles: 1类角色——承担剧情推进作用的角色及其故事发展
- secondary_roles: 2类角色——主要用于氛围铺垫/情绪渲染的角色及其作用
- protagonist_nodes: 主角（或窗口内主线推进者）的关键发展节点

产出存入独立影子表 story_arc_analysis，可增量补跑。
"""

import json
import logging
from typing import Awaitable, Callable

from src.db import arc_store, chapter_store
from src.infra import config as _cfg
from src.infra.llm_client import get_llm_client
from src.services.visualization_service import get_analyzed_range

logger = logging.getLogger(__name__)

WINDOW_SIZE = 20
# 每章上下文预算：头 2200 字 + 尾 800 字（冲突引入与收束钩子往往在章首尾）
HEAD_CHARS = 2200
TAIL_CHARS = 800

_ARC_SYSTEM_PROMPT = """你是资深网文编辑，负责把一段 20 章的情节窗口拆解为"可借鉴的大故事结构"。
输出必须是 JSON 对象，格式如下：
{
  "window_summary": "不超过150字的一句话总览（讲清这20章整体在干什么、叙事弧如何推进）",
  "primary_roles": [
    {"name": "角色名", "role": "该角色在这20章承担的剧情作用（一句话）", "development": "这20章内该角色从…到…的发展，写清关键转折发生在第几章"}
  ],
  "secondary_roles": [
    {"name": "角色名", "function": "该角色如何参与氛围铺垫/情绪渲染（一句话，写清具体场景）"}
  ],
  "protagonist_nodes": [
    {"chapter": 5, "event": "关键事件一句话", "impact": "对主角本人或主线推进的影响"}
  ]
}
要求：
1. primary_roles 2-5 条：指推动剧情发展的角色（含重要配角），development 必须具体到"谁做了什么导致什么"，不要写泛泛的"逐渐成长"。
2. secondary_roles 2-4 条：指主要承担氛围、情绪、背景铺垫的角色（如闲笔人物、环境衬托者），function 落到具体场景。
3. protagonist_nodes 3-8 个节点：主角的关键发展节点；若本窗口主角戏份少或为群像/配角窗口，就写窗口内的主线推进者节点，impact 说明其对主线的作用。
4. 控制篇幅：window_summary ≤150字，primary_roles.development 每条约60-100字，其余字段每条≤60字；宁可精简也不要截断。
5. 全部使用简体中文；角色名使用原文称呼；不要输出 JSON 以外的任何内容。"""

ProgressCb = Callable[[int, int], Awaitable[None]]


def window_ranges(novel_id: str, chapter_start: int, chapter_end: int) -> list[tuple[int, int]]:
    """Slice [chapter_start, chapter_end] into 20-chapter windows."""
    out: list[tuple[int, int]] = []
    for start in range(chapter_start, chapter_end + 1, WINDOW_SIZE):
        out.append((start, min(start + WINDOW_SIZE - 1, chapter_end)))
    return out


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


def _extract_arc(content: str | dict) -> dict | None:
    """Normalize LLM output into {window_summary, primary_roles, secondary_roles, protagonist_nodes}."""
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

    primary = _clean_list("primary_roles")
    secondary = _clean_list("secondary_roles")
    nodes = _clean_list("protagonist_nodes")

    if not summary and not primary and not secondary and not nodes:
        return None
    return {
        "window_summary": summary,
        "primary_roles": primary[:5],
        "secondary_roles": secondary[:4],
        "protagonist_nodes": nodes[:8],
    }


async def analyze_window(novel_id: str, arc_start: int, arc_end: int) -> dict | None:
    """Run story-arc analysis on one 20-chapter window. Returns saved record or None."""
    context = await _build_window_context(novel_id, arc_start, arc_end)
    if not context.strip():
        return None

    client = get_llm_client()
    prompt = (
        f"请拆解第{arc_start}-{arc_end}章这个大故事窗口（共 {arc_end - arc_start + 1} 章）。\n"
        f"以下是每章的章首与章尾节选：\n\n{context}"
    )
    raw, usage = await client.generate(
        system=_ARC_SYSTEM_PROMPT,
        prompt=prompt,
        format={"type": "json_object"},
        temperature=0.3,
        max_tokens=2000,
        timeout=180,
    )
    arc = _extract_arc(raw)
    if arc is None:
        logger.warning(
            "arc: failed to parse LLM output for window %s-%s: %r",
            arc_start, arc_end, str(raw)[:200],
        )
        return None

    await arc_store.save_arc(novel_id, arc_start, arc_end, arc, _cfg.LLM_MODEL)
    return {
        "arc_start": arc_start,
        "arc_end": arc_end,
        "arc": arc,
    }


async def analyze_all_windows(
    novel_id: str,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """Run story-arc analysis over all windows of the analyzed chapter range.

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
            logger.warning("arc: window %s-%s failed: %s", s, e, exc)
            ok = False
        results.append({"arc_start": s, "arc_end": e, "ok": ok})
        if ok:
            done += 1
        else:
            failed.append((s, e))
        if progress_cb is not None:
            await progress_cb(idx + 1, len(ranges))
    return {"windows": results, "done": done, "failed": failed}
