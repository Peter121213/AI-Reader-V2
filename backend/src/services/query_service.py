"""QA Pipeline: entity/keyword retrieval + LLM generation for novel Q&A."""

import json
import logging
import re
from collections.abc import AsyncIterator

from src.db import chapter_fact_store, chapter_store, conversation_store
from src.infra.llm_client import get_llm_client
from src.services import embedding_service, entity_aggregator
from src.services.alias_resolver import build_alias_map

logger = logging.getLogger(__name__)

_QA_SYSTEM_PROMPT = """你是一个专业的小说分析助手。你的任务是根据提供的小说知识库信息，回答用户关于小说内容的问题。

## 规则
1. **严格基于提供的知识库信息回答**，绝对不要使用你自己的知识补充任何人物、情节或关系。如果知识库中没有提到，就不要提及。
2. 回答时引用来源章节，格式为 [第X章]
3. 如果信息不足以回答问题，诚实说明"根据已分析的内容，暂未找到相关信息"，不要猜测或补充
4. 回答要简洁明了，重点突出
5. 在回答中提到人物、地点、物品等实体时，用其原名
6. 优先使用「人物档案」中的聚合关系数据，它比逐章碎片更准确完整
7. 如果「知识库信息」与问题无关或不足以回答，只允许说明暂未找到相关信息，禁止输出任何具体人物、情节或章节内容

## 知识库信息
{context}

## 对话历史
{history}

请严格基于以上知识库信息回答用户的问题。不要添加知识库中未提及的内容。"""

# Greeting / small-talk patterns — short-circuited before retrieval so that
# casual messages never trigger context injection (issue #56 symptom 3:
# "你好" LIKE-matched raw text from unanalyzed chapters).
_GREETING_RE = re.compile(
    r"^(你好|您好|hi|hello|嗨|喂|在吗|在么|早|早上好|上午好|下午好|晚上好|"
    r"谢谢|谢了|感谢|多谢|辛苦了|好的| ok |okay)[!！~～。.\s]*$",
    re.IGNORECASE,
)


def _is_greeting(question: str) -> bool:
    q = question.strip()
    return len(q) <= 12 and bool(_GREETING_RE.match(q))


# 分词后的通用疑问/虚词停用词表：仅用于关键词检索，不影响实体匹配。
# 只过滤纯虚词/疑问词，绝不过滤名词性内容词（判词/册子/金陵十二钗等）。
_QUESTION_STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "吗", "呢", "啊", "吧", "么", "呀", "哦",
    "什么", "哪里", "哪儿", "哪", "几", "哪章", "哪回", "第几", "多少",
    "请问", "知道", "不知道", "有没有", "是否", "是不是", "可以", "能",
    "能否", "告诉", "一下", "关于", "这个", "那个", "一个", "出现",
    "出现", "发生", "写", "讲", "说", "以及", "就是", "其实", "的话",
})


def _tokenize_question(question: str) -> list[str]:
    """中文分词：jieba 分词 + 停用词/标点过滤 + 去重保序。

    解决中文无空格导致整句被当作单一关键词、原文 LIKE 无法命中的问题。
    例如「太虚幻境中金陵十二钗册子上的诗在第几回」→
    ['太虚幻境', '金陵十二钗', '册子', '诗']，其中「金陵十二钗」可
    直接命中第 5 回原文。
    """
    try:
        import jieba
        words = [w.strip() for w in jieba.lcut(question)]
    except Exception:
        words = re.split(r"[\s，。！？、；：（）《》【】,.!?;:()\[\]<>·…—\-]+", question)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if len(w) < 2:
            continue
        if w in _QUESTION_STOPWORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


async def _expand_keywords(
    question: str,
    base_keywords: list[str],
) -> tuple[list[str], list[str]]:
    """LLM 查询扩展（通用机制，不依赖任何书籍特定知识）。

    问题词与原文词汇常失联（如「薛宝钗的判词」在原文中对应
    「金陵十二钗」「册子」）：让 LLM 推断书内可能出现的强关联词，
    再并入关键词做检索。返回 (合并后的关键词, 扩展出的新词)。

    任何失败均静默回退为原关键词，不影响主流程。
    """
    if not base_keywords:
        return base_keywords, []
    try:
        client = get_llm_client()
        system = (
            "你是小说内部检索助手。用户会给出一个关于某本小说的提问及其关键词。"
            "请推断书中可能出现的、与问题强相关的其他词汇：同义词、相关人物、"
            "相关地点、原文中可能出现的说法。只输出最多 3 个词，用逗号分隔，"
            "不要解释、不要编号、不要输出任何其他内容。"
        )
        prompt = f"问题：{question}\n关键词：{'、'.join(base_keywords)}"
        content, _ = await client.generate(
            system=system,
            prompt=prompt,
            temperature=0.2,
            max_tokens=80,
            timeout=30,
        )
        if isinstance(content, dict):
            return base_keywords, []
        extra: list[str] = []
        for part in re.split(r"[,，、;；\s]+", str(content).strip()):
            part = part.strip()
            if 1 < len(part) <= 8 and part not in base_keywords and part not in extra:
                extra.append(part)
        merged = base_keywords + extra[:3]
        return merged, extra[:3]
    except Exception:
        logger.debug("query expansion failed, fallback to base keywords", exc_info=True)
        return base_keywords, []


# 通用称谓/代词停用词表。这些词在问题中多为泛指（如"西游记的主人公是谁"），
# 若作为别名参与子串匹配会误命中无关实体（"主人公"误中别名"公主"）。
# 取舍：命中停用词表的一律跳过，宁可漏匹配也不错匹配——即使"陛下""菩萨"
# 等偶尔是真实角色名，该角色通常还有更长的本名可被匹配。
_ENTITY_STOPWORDS: frozenset[str] = frozenset({
    "公主", "先生", "老婆", "师父", "主人公", "主角", "老公", "老婆婆",
    "大王", "将军", "夫人", "小姐", "公子", "老爷", "太太", "姑娘",
    "和尚", "道人", "师傅", "徒弟", "女儿", "儿子", "父亲", "母亲",
    "哥哥", "姐姐", "弟弟", "妹妹", "孩子", "老人", "女人", "男人",
    "少年", "少女", "奴婢", "丫鬟", "奴才", "陛下", "皇上", "圣人",
    "真人", "菩萨",
})


def _resolve_question_entities(
    question: str,
    all_entities: set[str],
    alias_map: dict[str, str],
) -> list[str]:
    """Extract entity names from question, resolving aliases to canonical names."""
    found: list[str] = []
    seen_canonical: set[str] = set()
    # Check longest names first to avoid partial matches
    for name in sorted(all_entities, key=len, reverse=True):
        # 停用词不参与匹配：泛指称谓子串误命中的代价远大于漏匹配
        if name in _ENTITY_STOPWORDS:
            continue
        if name in question:
            canonical = alias_map.get(name, name)
            if canonical not in seen_canonical:
                found.append(canonical)
                seen_canonical.add(canonical)
    return found


def _extract_entities_from_question(question: str, all_entities: set[str]) -> list[str]:
    """Extract known entity names from a question string."""
    found = []
    for name in sorted(all_entities, key=len, reverse=True):
        if name in question:
            found.append(name)
    return found


def _build_entity_context(
    facts: list[dict],
    entity_names: list[str],
    max_chars: int = 4000,
) -> tuple[str, list[int]]:
    """Build context from chapter facts mentioning given entities."""
    chunks: list[str] = []
    source_chapters: set[int] = set()
    total = 0

    for fact_row in facts:
        fact = fact_row["fact"]
        chapter_id = fact.get("chapter_id", 0)
        relevant_parts: list[str] = []

        # Characters
        for ch in fact.get("characters", []):
            if ch.get("name") in entity_names:
                parts = [f"人物「{ch['name']}」"]
                if ch.get("new_aliases"):
                    parts.append(f"别名: {', '.join(ch['new_aliases'])}")
                if ch.get("appearance"):
                    parts.append(f"外貌: {ch['appearance']}")
                if ch.get("abilities_gained"):
                    for ab in ch["abilities_gained"]:
                        parts.append(f"能力: {ab.get('name', '')} ({ab.get('dimension', '')})")
                relevant_parts.append(" | ".join(parts))

        # Relationships
        for rel in fact.get("relationships", []):
            a, b = rel.get("person_a", ""), rel.get("person_b", "")
            if a in entity_names or b in entity_names:
                evidence = rel.get("evidence", "")
                relevant_parts.append(
                    f"关系: {a} → {b}: {rel.get('relation_type', '')} ({evidence[:80]})"
                )

        # Locations
        for loc in fact.get("locations", []):
            if loc.get("name") in entity_names:
                desc = loc.get("description", "") or ""
                relevant_parts.append(
                    f"地点「{loc['name']}」类型={loc.get('type', '')} {desc[:60]}"
                )

        # Item events
        for ie in fact.get("item_events", []):
            if ie.get("item_name") in entity_names or ie.get("actor") in entity_names:
                relevant_parts.append(
                    f"物品: {ie.get('actor', '')} {ie.get('action', '')} {ie.get('item_name', '')} "
                    f"({ie.get('description', '')[:60]})"
                )

        # Org events
        for oe in fact.get("org_events", []):
            if oe.get("org_name") in entity_names or oe.get("member") in entity_names:
                relevant_parts.append(
                    f"组织: {oe.get('member', '')} {oe.get('action', '')} {oe.get('org_name', '')} "
                    f"角色={oe.get('role', '')}"
                )

        # Events mentioning entities
        for evt in fact.get("events", []):
            participants = evt.get("participants", [])
            if any(p in entity_names for p in participants):
                relevant_parts.append(
                    f"事件[{evt.get('type', '')}]: {evt.get('summary', '')[:100]}"
                )

        # Concepts
        for nc in fact.get("new_concepts", []):
            if nc.get("name") in entity_names:
                relevant_parts.append(
                    f"概念「{nc['name']}」: {nc.get('definition', '')[:80]}"
                )

        if relevant_parts:
            block = f"[第{chapter_id}章] " + " ‖ ".join(relevant_parts)
            if total + len(block) > max_chars:
                break
            chunks.append(block)
            source_chapters.add(chapter_id)
            total += len(block)

    return "\n".join(chunks), sorted(source_chapters)


def _build_keyword_context(
    facts: list[dict],
    keywords: list[str],
    max_chars: int = 2000,
) -> tuple[str, list[int]]:
    """Build context from events/summaries matching keywords."""
    chunks: list[str] = []
    source_chapters: set[int] = set()
    total = 0

    for fact_row in facts:
        fact = fact_row["fact"]
        chapter_id = fact.get("chapter_id", 0)

        for evt in fact.get("events", []):
            summary = evt.get("summary", "")
            if any(kw in summary for kw in keywords):
                block = f"[第{chapter_id}章] 事件: {summary}"
                if total + len(block) > max_chars:
                    return "\n".join(chunks), sorted(source_chapters)
                chunks.append(block)
                source_chapters.add(chapter_id)
                total += len(block)

    return "\n".join(chunks), sorted(source_chapters)


# Question keywords → relation stage types to search for
_RELATION_QUERY_KEYWORDS: dict[str, list[str]] = {
    "亲密": ["恋人", "亲密", "爱慕", "情侣", "道侣", "夫妻", "单相思"],
    "恋人": ["恋人", "亲密", "爱慕", "情侣", "道侣", "夫妻", "单相思"],
    "女朋友": ["恋人", "亲密", "爱慕", "情侣", "道侣", "夫妻", "单相思"],
    "感情": ["恋人", "亲密", "爱慕", "情侣", "道侣", "夫妻", "单相思"],
    "师徒": ["师徒", "师生", "传授", "拜师"],
    "师父": ["师徒", "师生", "传授", "拜师"],
    "敌人": ["敌对", "仇敌", "对手"],
    "仇人": ["敌对", "仇敌", "仇恨"],
    "朋友": ["朋友", "同伴", "同门", "知己"],
    "家人": ["父子", "母子", "兄弟", "兄妹", "姐弟", "夫妻", "叔侄"],
    "亲属": ["父子", "母子", "兄弟", "兄妹", "姐弟", "夫妻", "叔侄"],
}


def _detect_relation_filter(question: str) -> list[str] | None:
    """Detect if question asks about specific relation types.

    Returns a list of stage type keywords to match, or None if no filter.
    """
    for keyword, stage_types in _RELATION_QUERY_KEYWORDS.items():
        if keyword in question:
            return stage_types
    return None


async def _build_profile_context(
    novel_id: str,
    person_names: list[str],
    question: str = "",
    max_chars: int = 4000,
) -> tuple[str, list[int]]:
    """Build structured context from aggregated PersonProfile data.

    Uses EntityAggregator to get cross-chapter merged relations with
    normalized types and category classification. When the question asks
    about specific relation types, scans ALL stages (not just category)
    to find matching relations.
    """
    chunks: list[str] = []
    source_chapters: set[int] = set()
    total = 0
    relation_filter = _detect_relation_filter(question)

    for name in person_names[:5]:
        try:
            profile = await entity_aggregator.aggregate_person(novel_id, name)
        except Exception as e:
            logger.debug("Failed to aggregate person %s: %s", name, e)
            continue

        parts: list[str] = [f"## 人物「{profile.name}」"]

        if profile.aliases:
            alias_names = [a.name for a in profile.aliases[:10]]
            parts.append(f"别名: {', '.join(alias_names)}")

        if profile.relations:
            if relation_filter:
                # Question-aware: scan ALL relations for matching stages
                parts.append(f"【与「{question}」相关的关系】")
                matched_count = 0
                for rc in profile.relations:
                    matching_stages = [
                        s for s in rc.stages
                        if any(kw in s.relation_type for kw in relation_filter)
                    ]
                    if matching_stages:
                        for stage in matching_stages:
                            ch_refs = ", ".join(
                                f"第{c}章" for c in stage.chapters[:5]
                            )
                            ev = stage.evidences[0][:100] if stage.evidences else ""
                            parts.append(
                                f"  {rc.other_person}: {stage.relation_type} "
                                f"[{ch_refs}] {ev}"
                            )
                            source_chapters.update(stage.chapters)
                        matched_count += 1
                    if matched_count >= 20:
                        break
            else:
                # General: group by category, show top relations per category
                cat_groups: dict[str, list[str]] = {}
                for rc in profile.relations:
                    cat = rc.category or "other"
                    # Only include the most recent / representative stage
                    stage = rc.stages[-1] if rc.stages else None
                    if stage:
                        ch_refs = ", ".join(
                            f"第{c}章" for c in stage.chapters[:3]
                        )
                        ev = stage.evidences[0][:80] if stage.evidences else ""
                        line = (
                            f"  {rc.other_person}: {stage.relation_type} "
                            f"[{ch_refs}] {ev}"
                        )
                        cat_groups.setdefault(cat, []).append(line)

                cat_labels = {
                    "family": "亲属关系",
                    "intimate": "亲密关系",
                    "hierarchical": "师从/主从关系",
                    "social": "社交关系",
                    "hostile": "敌对关系",
                    "other": "其他关系",
                }
                for cat in ["family", "intimate", "hierarchical",
                            "social", "hostile", "other"]:
                    lines = cat_groups.get(cat, [])
                    if lines:
                        label = cat_labels[cat]
                        parts.append(f"【{label}】({len(lines)}条)")
                        parts.extend(lines[:8])

        if profile.abilities:
            ab_strs = [
                f"{a.name}({a.dimension})" for a in profile.abilities[:5]
            ]
            parts.append(f"能力: {', '.join(ab_strs)}")

        block = "\n".join(parts)
        if total + len(block) > max_chars:
            break
        chunks.append(block)
        total += len(block)

    return "\n\n".join(chunks), sorted(source_chapters)


async def _build_text_context(
    novel_id: str,
    keywords: list[str],
    max_results: int = 5,
    deep_read_chapters: int = 2,
    deep_read_chars: int = 6000,
    priority_keywords: list[str] | None = None,
) -> tuple[str, list[int]]:
    """Build context from chapter text search.

    命中章节分两级（通用机制，不依赖任何书籍特定知识）：
    - 精读：按优先级取前 deep_read_chapters 章，纳入该章完整原文
      （最多 deep_read_chars 字符）。解决关键词命中位置在章首、而目标
      内容（如判词）在章中部、±50 字 snippet 覆盖不到的漏检问题——
      让 LLM 基于完整章节自行理解定位。
    - 浏览：其余命中章仅取命中处 ±50 字 snippet，控制上下文体积。

    优先级：priority_keywords（查询扩展词）命中的章节排在最前——
    扩展词通常精准指向目标内容所在章节。
    """
    chunks: list[str] = []
    source_chapters: set[int] = set()

    # 第一遍：按关键词收集全部命中，记录每个关键词命中了哪些章节
    per_chapter_hits: dict[int, list[dict]] = {}
    kw_hit_chapters: dict[str, set[int]] = {}
    for kw in keywords[:4]:
        results = await chapter_store.search_chapters(novel_id, kw, limit=max_results)
        kw_hit_chapters[kw] = {r["chapter_num"] for r in results}
        for r in results:
            per_chapter_hits.setdefault(r["chapter_num"], []).append(r)

    if not per_chapter_hits:
        return "", []

    priority_set = set(priority_keywords or [])
    # 扩展词命中过的章节视为高优先级
    prio_chapters: set[int] = set()
    for pk in priority_set:
        prio_chapters |= kw_hit_chapters.get(pk, set())

    ranked = sorted(
        per_chapter_hits.items(),
        key=lambda kv: (kv[0] in prio_chapters, len(kv[1]), kv[0]),
        reverse=True,
    )

    # 精读前 N 章完整原文
    for ch_num, hits in ranked[:deep_read_chapters]:
        row = await chapter_store.get_chapter_content(novel_id, ch_num)
        full_text = (row or {}).get("content", "")
        if not full_text:
            continue
        snippet = full_text[:deep_read_chars]
        block = f"[第{ch_num}章 全文]\n{snippet}"
        chunks.append(block)
        source_chapters.add(ch_num)

    # 其余命中章：±50 字 snippet
    for ch_num, hits in ranked[deep_read_chapters:]:
        if ch_num in source_chapters:
            continue
        for r in hits[:1]:
            block = f"[第{ch_num}章] {r.get('title', '')}: ...{r['snippet']}..."
            chunks.append(block)
            source_chapters.add(ch_num)

    return "\n\n".join(chunks), sorted(source_chapters)


def _collect_all_entity_names(facts: list[dict]) -> set[str]:
    """Collect all entity names from all facts."""
    names: set[str] = set()
    for fact_row in facts:
        fact = fact_row["fact"]
        for ch in fact.get("characters", []):
            if ch.get("name"):
                names.add(ch["name"])
        for loc in fact.get("locations", []):
            if loc.get("name"):
                names.add(loc["name"])
        for ie in fact.get("item_events", []):
            if ie.get("item_name"):
                names.add(ie["item_name"])
        for oe in fact.get("org_events", []):
            if oe.get("org_name"):
                names.add(oe["org_name"])
        for nc in fact.get("new_concepts", []):
            if nc.get("name"):
                names.add(nc["name"])
    return names


def _build_history_text(messages: list[dict], max_turns: int = 5) -> str:
    """Build conversation history text from recent messages."""
    if not messages:
        return "（无历史对话）"
    recent = messages[-(max_turns * 2):]
    lines = []
    for msg in recent:
        role = "用户" if msg["role"] == "user" else "助手"
        content = msg["content"][:200]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _extract_source_chapters(answer: str) -> list[int]:
    """Extract [第X章] references from the answer text."""
    matches = re.findall(r"第(\d+)章", answer)
    return sorted(set(int(m) for m in matches))


async def _stream_final_answer(
    novel_id: str,
    question: str,
    conversation_id: str | None,
    context: str,
    all_source_chapters: set[int],
    analyzed_count: int,
) -> AsyncIterator[dict]:
    """Final answer generation shared by the RAG pipeline and agent QA.

    Streams tokens via _QA_SYSTEM_PROMPT, extracts source chapters from the
    answer, and persists the exchange to the conversation store.
    """
    llm = get_llm_client()

    # Build conversation history
    history_text = "（无历史对话）"
    if conversation_id:
        recent = await conversation_store.get_recent_messages(conversation_id, limit=6)
        history_text = _build_history_text(recent)

    # Build final prompt
    system_prompt = _QA_SYSTEM_PROMPT.format(
        context=context,
        history=history_text,
    )

    user_prompt = f"{question}\n\n（注：当前已分析 {analyzed_count} 章内容）"

    # Stream LLM response
    full_answer = ""
    try:
        async for token in llm.generate_stream(
            system=system_prompt,
            prompt=user_prompt,
            timeout=180,
        ):
            full_answer += token
            yield {"type": "token", "content": token}
    except Exception as e:
        logger.error(f"LLM streaming error: {e}")
        error_msg = "抱歉，生成回答时出现错误，请稍后重试。"
        yield {"type": "token", "content": error_msg}
        full_answer = error_msg

    # Extract source chapters from answer
    answer_sources = _extract_source_chapters(full_answer)
    # Merge with retrieval sources
    final_sources = sorted(set(answer_sources) | all_source_chapters)

    yield {"type": "sources", "chapters": final_sources}
    yield {"type": "done"}

    # Save messages to DB if conversation exists
    if conversation_id:
        try:
            await conversation_store.add_message(
                conversation_id, "user", question
            )
            await conversation_store.add_message(
                conversation_id,
                "assistant",
                full_answer,
                sources_json=json.dumps(final_sources),
            )
        except Exception as e:
            logger.error(f"Failed to save messages: {e}")


async def query_stream(
    novel_id: str,
    question: str,
    conversation_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream QA response.

    Yields dicts:
      {"type": "status", "content": str}    — agent forensic step (agent mode only)
      {"type": "token", "content": str}     — streamed answer tokens
      {"type": "sources", "chapters": [...]} — source chapters when done
      {"type": "done"}                       — signal completion
    """
    # Agentic mode: tool-use forensics loop. Falls back to the RAG pipeline
    # below on any failure (unsupported provider, tool-calling error, etc.).
    from src.infra import config as _cfg  # dynamic read (runtime mode switches)

    if _cfg.QA_MODE == "agent" and _cfg.LLM_PROVIDER != "ollama":
        from src.services import agent_qa_service

        try:
            async for chunk in agent_qa_service.agent_query_stream(
                novel_id=novel_id,
                question=question,
                conversation_id=conversation_id,
            ):
                yield chunk
            return
        except Exception as e:
            logger.warning("Agent QA failed, falling back to RAG pipeline: %s", e)

    # 1. Load all chapter facts for the novel
    all_facts = await chapter_fact_store.get_all_chapter_facts(novel_id)
    if not all_facts:
        yield {"type": "token", "content": "该小说尚未进行分析，请先分析后再提问。"}
        yield {"type": "sources", "chapters": []}
        yield {"type": "done"}
        return

    analyzed_count = len(all_facts)

    # Small talk: reply with a fixed guide, never touch retrieval/LLM
    if _is_greeting(question):
        yield {
            "type": "token",
            "content": (
                f"你好！我是这本小说的知识库助手，当前已分析 {analyzed_count} 章。"
                "你可以问我已分析内容里的人物、地点、事件等问题，"
                "比如「孙悟空和唐僧是什么关系」「第 5 章发生了什么」。"
            ),
        }
        yield {"type": "sources", "chapters": []}
        yield {"type": "done"}
        return

    # 2. Extract entities from question (with alias resolution)
    all_entity_names = _collect_all_entity_names(all_facts)
    try:
        alias_map = await build_alias_map(novel_id)
    except Exception:
        alias_map = {}
    # Add alias keys to entity name pool so aliases in questions get matched
    all_entity_names.update(alias_map.keys())
    question_entities = _resolve_question_entities(
        question, all_entity_names, alias_map,
    )

    # 3. Build context from multiple sources
    context_parts: list[str] = []
    all_source_chapters: set[int] = set()

    # Aggregated profile retrieval (highest priority — cross-chapter merged)
    profiled_names: set[str] = set()
    if question_entities:
        profile_ctx, profile_chs = await _build_profile_context(
            novel_id, question_entities, question=question,
        )
        if profile_ctx:
            context_parts.append("### 人物档案（跨章节聚合）\n" + profile_ctx)
            all_source_chapters.update(profile_chs)
            profiled_names.update(question_entities)

    # Entity-based retrieval from raw chapter facts (supplementary)
    # Skip entities already covered by profile context to reduce noise
    remaining_entities = [e for e in question_entities if e not in profiled_names]
    if remaining_entities:
        entity_ctx, entity_chs = _build_entity_context(all_facts, remaining_entities)
        if entity_ctx:
            context_parts.append("### 实体相关信息\n" + entity_ctx)
            all_source_chapters.update(entity_chs)

    # Keyword retrieval from events
    keywords = _tokenize_question(question)
    if not keywords:
        keywords = [question]
    # LLM 查询扩展（通用）：补入与问题强相关的书内关联词
    keywords, extra_keywords = await _expand_keywords(question, keywords)
    keyword_ctx, kw_chs = _build_keyword_context(all_facts, keywords)
    if keyword_ctx:
        context_parts.append("### 相关事件\n" + keyword_ctx)
        all_source_chapters.update(kw_chs)

    # Semantic search via ChromaDB embeddings
    try:
        semantic_results = embedding_service.search_chapters(novel_id, question, n_results=5)
        if semantic_results:
            sem_chunks = []
            for sr in semantic_results:
                ch_num = sr["chapter_num"]
                if ch_num not in all_source_chapters:
                    # Truncate document to first 200 chars
                    doc_snippet = sr["document"][:200]
                    sem_chunks.append(f"[第{ch_num}章] {doc_snippet}")
                    all_source_chapters.add(ch_num)
            if sem_chunks:
                context_parts.append("### 语义相关段落\n" + "\n".join(sem_chunks))
    except Exception as e:
        # Must stay visible: a silent embedding failure degrades QA to
        # "暂无相关知识库信息" with no trace (issue #56 symptom 1)
        logger.warning("Semantic search unavailable: %s", e)

    # Full-text search in chapter content
    text_ctx, text_chs = await _build_text_context(
        novel_id, keywords, priority_keywords=extra_keywords,
    )
    if text_ctx:
        context_parts.append("### 原文片段\n" + text_ctx)
        all_source_chapters.update(text_chs)

    # Retrieval found nothing: reply with a fixed message instead of calling
    # the LLM on an empty context (empty context invites hallucination,
    # issue #56 symptom 1/3)
    if not context_parts:
        yield {
            "type": "token",
            "content": (
                f"当前已分析 {analyzed_count} 章，但未从已分析内容中检索到与你问题相关的信息。"
                "相关内容可能在尚未分析的章节，也可以换种问法（提及具体人物/地点名）再试。"
            ),
        }
        yield {"type": "sources", "chapters": []}
        yield {"type": "done"}
        return

    context = "\n\n".join(context_parts)

    # 4. Final generation (shared with agent QA mode)
    async for chunk in _stream_final_answer(
        novel_id=novel_id,
        question=question,
        conversation_id=conversation_id,
        context=context,
        all_source_chapters=all_source_chapters,
        analyzed_count=analyzed_count,
    ):
        yield chunk
