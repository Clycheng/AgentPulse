"""TD-13 company world model and evidence-backed employee memory.

The raw company event ledger is shared and append-only. Employee memories are
separate projections: they can contain private interpretations, but every
promoted conclusion must point back to one or more company events. This keeps
the cognitive layer inspectable without turning the whole transcript into a
permanent prompt.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from app.core.database import Database, Row
from app.services.workspace import new_id, now_iso


MEMORY_TYPES = ("observation", "episode", "reflection", "relationship", "lesson")
_STOP_WORDS = {
    "一个", "一些", "这个", "那个", "现在", "需要", "负责", "公司", "工作",
    "任务", "内容", "进行", "开始", "帮我", "可以", "然后", "以及", "请你",
    "the", "and", "for", "with", "this", "that", "from", "what", "your",
}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _tokens(value: str) -> set[str]:
    if not value:
        return set()
    tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", value.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        for size in (2, 3, 4):
            for index in range(0, len(chunk) - size + 1):
                token = chunk[index : index + size]
                if token not in _STOP_WORDS:
                    tokens.add(token)
    return {token for token in tokens if token not in _STOP_WORDS}


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _recency(value: str | None) -> float:
    age_days = max(0.0, (datetime.now(UTC) - _parse_time(value)).total_seconds() / 86400)
    return math.exp(-age_days / 45.0)


def record_company_event(
    conn: Database,
    *,
    workspace_id: str,
    event_type: str,
    source_id: str,
    title: str = "",
    content: str = "",
    conversation_id: str | None = None,
    task_id: str | None = None,
    actor_agent_id: str | None = None,
    actor_user_id: str | None = None,
    occurred_at: str | None = None,
    importance: float = 1.0,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> dict:
    """Insert one immutable event idempotently and return its public row."""
    existing = conn.execute(
        "SELECT * FROM company_events WHERE workspace_id = ? AND event_type = ? AND source_id = ?",
        (workspace_id, event_type, source_id),
    ).fetchone()
    if existing is not None:
        return dict(existing)

    event_id = new_id("ce")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO company_events (
          id, workspace_id, event_type, source_id, conversation_id, task_id,
          actor_agent_id, actor_user_id, title, content, occurred_at,
          importance, confidence, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            workspace_id,
            event_type,
            source_id,
            conversation_id,
            task_id,
            actor_agent_id,
            actor_user_id,
            (title or "")[:240],
            (content or "")[:12000],
            occurred_at or created_at,
            max(0.0, min(float(importance), 10.0)),
            max(0.0, min(float(confidence), 1.0)),
            _json(metadata or {}),
            created_at,
        ),
    )
    return dict(conn.execute("SELECT * FROM company_events WHERE id = ?", (event_id,)).fetchone())


def _agent_label(conn: Database, agent_id: str | None) -> str:
    if not agent_id:
        return "系统"
    row = conn.execute("SELECT name, role FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        return "同事"
    return f"{row['name']}（{row['role']}）" if row["role"] else row["name"]


def _user_label(conn: Database, user_id: str | None) -> str:
    if not user_id:
        return "系统"
    row = conn.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    return (row["display_name"] if row and row["display_name"] else "同事")


def _event_actor(conn: Database, row: dict) -> str:
    if row.get("actor_agent_id"):
        return _agent_label(conn, row["actor_agent_id"])
    if row.get("actor_user_id"):
        return _user_label(conn, row["actor_user_id"])
    return "系统"


def _relationship_event(
    conn: Database,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_agent_id: str,
    event_id: str,
    occurred_at: str,
) -> None:
    members = conn.execute(
        "SELECT agent_id FROM conversation_members WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchall()
    colleagues = [row["agent_id"] for row in members if row["agent_id"] != actor_agent_id]
    for colleague_id in colleagues:
        existing = conn.execute(
            """SELECT * FROM agent_relationships
            WHERE workspace_id = ? AND agent_id = ? AND colleague_agent_id = ?""",
            (workspace_id, actor_agent_id, colleague_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO agent_relationships (
                  id, workspace_id, agent_id, colleague_agent_id, summary,
                  trust_score, interaction_count, evidence_event_ids_json,
                  last_interacted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0.5, 1, ?, ?, ?)""",
                (
                    new_id("rel"),
                    workspace_id,
                    actor_agent_id,
                    colleague_id,
                    f"与 {_agent_label(conn, colleague_id)} 有过一次公司协作。",
                    _json([event_id]),
                    occurred_at,
                    now_iso(),
                ),
            )
            continue
        try:
            evidence = json.loads(existing["evidence_event_ids_json"] or "[]")
        except (TypeError, ValueError):
            evidence = []
        evidence = (evidence + [event_id])[-20:]
        conn.execute(
            """UPDATE agent_relationships SET interaction_count = interaction_count + 1,
            last_interacted_at = ?, updated_at = ?, evidence_event_ids_json = ?
            WHERE id = ?""",
            (occurred_at, now_iso(), _json(evidence), existing["id"]),
        )


def record_message_event(conn: Database, row: Row) -> dict:
    """Project a stored message into the shared world model."""
    conversation = conn.execute(
        "SELECT workspace_id FROM conversations WHERE id = ?",
        (row["conversation_id"],),
    ).fetchone()
    if conversation is None:
        raise ValueError("message conversation not found")
    sender_agent_id = None
    sender_user_id = None
    if row["sender_type"] == "agent":
        sender_agent_id = conn.execute(
            "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
            (row["sender_id"], conversation["workspace_id"]),
        ).fetchone()
        sender_agent_id = sender_agent_id["id"] if sender_agent_id else None
    elif row["sender_type"] == "user":
        sender_user_id = conn.execute(
            "SELECT id FROM users WHERE id = ?", (row["sender_id"],)
        ).fetchone()
        sender_user_id = sender_user_id["id"] if sender_user_id else None
    event = record_company_event(
        conn,
        workspace_id=conversation["workspace_id"],
        event_type="message",
        source_id=row["id"],
        title="会话消息",
        content=row["content"],
        conversation_id=row["conversation_id"],
        actor_agent_id=sender_agent_id,
        actor_user_id=sender_user_id,
        occurred_at=row["created_at"],
        importance=2.0 if row["sender_type"] == "user" else 1.0,
        metadata={"sender_type": row["sender_type"]},
    )
    if sender_agent_id:
        _relationship_event(
            conn,
            workspace_id=event["workspace_id"],
            conversation_id=row["conversation_id"],
            actor_agent_id=sender_agent_id,
            event_id=event["id"],
            occurred_at=row["created_at"],
        )
    return event


def record_task_event_projection(conn: Database, row: Row, *, event_type: str = "task_event") -> dict:
    return record_company_event(
        conn,
        workspace_id=row["workspace_id"],
        event_type=event_type,
        source_id=row["id"],
        title=row["title"],
        content=row["content"],
        conversation_id=row["conversation_id"],
        task_id=row["task_id"],
        actor_agent_id=row["agent_id"],
        occurred_at=row["created_at"],
        importance=2.0,
    )


def record_memory(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str,
    memory_type: str,
    title: str,
    content: str,
    evidence_event_ids: list[str] | None = None,
    importance: float = 1.0,
    confidence: float = 1.0,
    is_private: bool = True,
    promoted: bool = False,
) -> dict:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"invalid memory type: {memory_type}")
    memory_id = new_id("mem")
    created_at = now_iso()
    conn.execute(
        """INSERT INTO agent_memories (
          id, workspace_id, agent_id, memory_type, title, content,
          importance, confidence, is_private, promoted, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_id,
            workspace_id,
            agent_id,
            memory_type,
            (title or "")[:240],
            (content or "")[:12000],
            max(0.0, min(float(importance), 10.0)),
            max(0.0, min(float(confidence), 1.0)),
            1 if is_private else 0,
            1 if promoted else 0,
            created_at,
            created_at,
        ),
    )
    for event_id in evidence_event_ids or []:
        exists = conn.execute(
            "SELECT id FROM company_events WHERE id = ? AND workspace_id = ?",
            (event_id, workspace_id),
        ).fetchone()
        if exists is None:
            continue
        conn.execute(
            """INSERT INTO memory_links (
              id, workspace_id, memory_id, event_id, relation, created_at
            ) VALUES (?, ?, ?, ?, 'evidence', ?)""",
            (new_id("mlink"), workspace_id, memory_id, event_id, created_at),
        )
    row = conn.execute("SELECT * FROM agent_memories WHERE id = ?", (memory_id,)).fetchone()
    return dict(row)


def _score(content: str, query_tokens: set[str], occurred_at: str, importance: float, confidence: float) -> float:
    haystack = (content or "").lower()
    matched = sum(1 for token in query_tokens if token in haystack)
    lexical = matched / max(1, len(query_tokens))
    return lexical * 5.0 + _recency(occurred_at) + float(importance) * 0.35 + float(confidence) * 0.5


def search_company_memory(
    conn: Database,
    *,
    workspace_id: str,
    query: str,
    agent_id: str | None = None,
    limit: int = 12,
) -> list[dict]:
    """Hybrid portable retrieval for company facts and employee memories."""
    limit = max(1, min(int(limit), 50))
    query_tokens = _tokens(query)
    events = conn.execute(
        """SELECT * FROM company_events WHERE workspace_id = ?
        ORDER BY occurred_at DESC LIMIT 500""",
        (workspace_id,),
    ).fetchall()
    memories = conn.execute(
        """SELECT * FROM agent_memories
        WHERE workspace_id = ? AND (
          (agent_id = ? AND is_private = 1) OR promoted = 1 OR is_private = 0
        )
        ORDER BY updated_at DESC LIMIT 300""",
        (workspace_id, agent_id or ""),
    ).fetchall()
    ranked: list[dict] = []
    for row in events:
        item = dict(row)
        item.update(
            kind="event",
            score=_score(
                f"{row['title']} {row['content']}", query_tokens,
                row["occurred_at"], row["importance"], row["confidence"],
            ),
        )
        ranked.append(item)
    for row in memories:
        item = dict(row)
        item.update(
            kind="memory",
            score=_score(
                f"{row['title']} {row['content']}", query_tokens,
                row["updated_at"], row["importance"], row["confidence"],
            ),
        )
        ranked.append(item)
    ranked.sort(key=lambda item: (item["score"], item.get("occurred_at") or item.get("updated_at") or ""), reverse=True)
    selected = ranked[:limit]
    for item in selected:
        if item["kind"] == "memory":
            conn.execute(
                "UPDATE agent_memories SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
                (now_iso(), item["id"]),
            )
    return selected


def _public_event_line(conn: Database, row: dict) -> str:
    actor = _event_actor(conn, row)
    title = row.get("title") or "公司记录"
    content = (row.get("content") or "").strip().replace("\n", " ")[:900]
    evidence = row.get("id") or row.get("source_id") or "unknown"
    return f"- {actor}：{title}。{content}（证据 {evidence}）"


def _memory_line(row: dict) -> str:
    title = row.get("title") or "经验"
    content = (row.get("content") or "").strip().replace("\n", " ")[:900]
    return f"- {title}：{content}（记忆 {row.get('id', 'unknown')}）"


def _ensure_conversation_episode(
    conn: Database, *, workspace_id: str, conversation_id: str, agent_id: str
) -> None:
    """Compact older transcript slices while retaining every raw message event."""
    messages = conn.execute(
        """SELECT m.id, m.content FROM messages m
        WHERE m.conversation_id = ? ORDER BY m.created_at, m.id""",
        (conversation_id,),
    ).fetchall()
    if len(messages) <= 24:
        return
    slice_rows = messages[:-12][-12:]
    if not slice_rows:
        return
    title = f"会话经历 {slice_rows[0]['id']} - {slice_rows[-1]['id']}"
    existing = conn.execute(
        """SELECT id FROM agent_memories
        WHERE workspace_id = ? AND agent_id = ? AND memory_type = 'episode'
          AND title = ? LIMIT 1""",
        (workspace_id, agent_id, title),
    ).fetchone()
    if existing is not None:
        return
    evidence_rows = conn.execute(
        """SELECT id, source_id FROM company_events
        WHERE workspace_id = ? AND event_type = 'message'
          AND source_id IN ({})""".format(",".join("?" for _ in slice_rows)),
        (workspace_id, *(row["id"] for row in slice_rows)),
    ).fetchall()
    evidence_ids = [row["id"] for row in evidence_rows]
    if not evidence_ids:
        return
    summary = "；".join(
        row["content"].replace("\n", " ")[:320] for row in slice_rows
    )
    record_memory(
        conn,
        workspace_id=workspace_id,
        agent_id=agent_id,
        memory_type="episode",
        title=title,
        content=summary[:5000],
        evidence_event_ids=evidence_ids,
        importance=2.5,
        confidence=0.85,
    )


def build_context_manifest(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    agent: Row,
    current_text: str,
    task_id: str | None = None,
    discussion_context: str = "",
    token_budget: int = 12000,
) -> dict:
    """Build and persist one employee-specific context working set."""
    _ensure_conversation_episode(
        conn,
        workspace_id=workspace["id"],
        conversation_id=conversation["id"],
        agent_id=agent["id"],
    )
    query = " ".join(
        value for value in (
            current_text,
            conversation["name"] if conversation else "",
            discussion_context,
        ) if value
    )
    focus = [query[:1000]]
    if discussion_context and discussion_context[:1000] not in focus:
        focus.append(discussion_context[:1000])
    selected = search_company_memory(
        conn,
        workspace_id=workspace["id"],
        query=query,
        agent_id=agent["id"],
        limit=24,
    )
    events = [item for item in selected if item["kind"] == "event"][:14]
    memories = [item for item in selected if item["kind"] == "memory"][:10]

    event_lines = [_public_event_line(conn, item) for item in events]
    memory_lines = [_memory_line(item) for item in memories]
    sections = [
        "【公司记忆：与当前问题相关的事实】\n" + ("\n".join(event_lines) or "- 暂无相关公司事实"),
        "【当前员工的经验与判断】\n" + ("\n".join(memory_lines) or "- 暂无已沉淀经验"),
    ]
    relationship_rows = conn.execute(
        """SELECT r.summary, a.name, a.role FROM agent_relationships r
        JOIN agents a ON a.id = r.colleague_agent_id
        WHERE r.workspace_id = ? AND r.agent_id = ?
        ORDER BY r.last_interacted_at DESC LIMIT 8""",
        (workspace["id"], agent["id"]),
    ).fetchall()
    relationship_lines = [
        f"- {row['name']}（{row['role']}）：{row['summary']}" for row in relationship_rows
    ]
    sections.append(
        "【同事协作记忆】\n" + ("\n".join(relationship_lines) or "- 暂无")
    )
    if discussion_context:
        sections.append("【当前讨论约束】\n" + discussion_context[:4000])
    context_text = "\n\n".join(sections)
    max_chars = max(12000, min(token_budget * 4, 48000))
    context_text = context_text[:max_chars]
    prompt_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
    manifest_id = new_id("ctx")
    conn.execute(
        """INSERT INTO context_manifests (
          id, workspace_id, conversation_id, task_id, agent_id, query,
          focus_json, selected_event_ids_json, selected_memory_ids_json,
          prompt_hash, token_budget, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            manifest_id,
            workspace["id"],
            conversation["id"] if conversation else None,
            task_id,
            agent["id"],
            query[:4000],
            _json(focus),
            _json([item["id"] for item in events]),
            _json([item["id"] for item in memories]),
            prompt_hash,
            token_budget,
            now_iso(),
        ),
    )
    return {
        "id": manifest_id,
        "text": context_text,
        "focus": focus,
        "events": events,
        "memories": memories,
        "prompt_hash": prompt_hash,
    }


def attach_manifest_to_run(conn: Database, manifest_id: str, run_id: str) -> None:
    conn.execute("UPDATE context_manifests SET run_id = ? WHERE id = ?", (run_id, manifest_id))


def record_run_observation(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str,
    run_id: str,
    conversation_id: str | None,
    task_id: str | None,
    reply: str,
    status: str,
) -> dict | None:
    if not reply.strip():
        return None
    event = record_company_event(
        conn,
        workspace_id=workspace_id,
        event_type="run_result",
        source_id=run_id,
        title="员工完成一轮工作",
        content=reply,
        conversation_id=conversation_id,
        task_id=task_id,
        actor_agent_id=agent_id,
        importance=3.0 if status == "completed" else 2.0,
        metadata={"status": status},
    )
    return record_memory(
        conn,
        workspace_id=workspace_id,
        agent_id=agent_id,
        memory_type="observation",
        title="最近一次工作观察",
        content=reply[:2000],
        evidence_event_ids=[event["id"]],
        importance=2.0,
        confidence=0.8,
    )


def reflect_agent_memories(conn: Database, *, workspace_id: str, agent_id: str) -> dict | None:
    """Create a deterministic evidence-backed reflection for idle/manual ticks.

    This first implementation intentionally does not make another hidden model
    call. It compacts recent observations while preserving their evidence. A
    later provider-specific worker can replace only this function's synthesis
    step without changing the ledger or retrieval contract.
    """
    rows = conn.execute(
        """SELECT m.* FROM agent_memories m
        WHERE m.workspace_id = ? AND m.agent_id = ? AND m.memory_type = 'observation'
        ORDER BY m.created_at DESC LIMIT 5""",
        (workspace_id, agent_id),
    ).fetchall()
    if len(rows) < 2:
        return None
    evidence = []
    for row in rows:
        linked = conn.execute(
            "SELECT event_id FROM memory_links WHERE memory_id = ? ORDER BY created_at DESC LIMIT 3",
            (row["id"],),
        ).fetchall()
        evidence.extend(item["event_id"] for item in linked)
    snippets = [row["content"].replace("\n", " ")[:300] for row in rows]
    reflection_content = "近期工作呈现出这些可复用事实：" + "；".join(snippets)
    previous = conn.execute(
        """SELECT id FROM agent_memories
        WHERE workspace_id = ? AND agent_id = ? AND memory_type = 'reflection'
          AND content = ? LIMIT 1""",
        (workspace_id, agent_id, reflection_content),
    ).fetchone()
    if previous is not None:
        return None
    reflection = record_memory(
        conn,
        workspace_id=workspace_id,
        agent_id=agent_id,
        memory_type="reflection",
        title="近期工作反思",
        content=reflection_content,
        evidence_event_ids=list(dict.fromkeys(evidence)),
        importance=3.0,
        confidence=0.7,
        is_private=True,
    )
    return reflection


def run_memory_reflection_tick(conn: Database, *, limit: int = 24) -> int:
    """Compact recent evidence for employees without exposing raw thoughts.

    This is intentionally a durable, deterministic worker step. It can later
    be replaced by a provider-backed synthesis call while keeping evidence and
    visibility rules unchanged.
    """
    rows = conn.execute(
        """SELECT id, workspace_id FROM agents
        ORDER BY created_at, id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    created = 0
    for row in rows:
        if reflect_agent_memories(
            conn, workspace_id=row["workspace_id"], agent_id=row["id"]
        ):
            created += 1
    return created


def list_memories(conn: Database, *, workspace_id: str, agent_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM agent_memories WHERE workspace_id = ? AND agent_id = ?
        ORDER BY updated_at DESC LIMIT ?""",
        (workspace_id, agent_id, max(1, min(limit, 100))),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["evidence_event_ids"] = [
            link["event_id"] for link in conn.execute(
                "SELECT event_id FROM memory_links WHERE memory_id = ? ORDER BY created_at",
                (row["id"],),
            ).fetchall()
        ]
        result.append(item)
    return result


def list_relationships(conn: Database, *, workspace_id: str, agent_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, a.name AS colleague_name, a.role AS colleague_role
        FROM agent_relationships r JOIN agents a ON a.id = r.colleague_agent_id
        WHERE r.workspace_id = ? AND r.agent_id = ?
        ORDER BY r.last_interacted_at DESC, r.updated_at DESC""",
        (workspace_id, agent_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_events(conn: Database, *, workspace_id: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM company_events WHERE workspace_id = ?
        ORDER BY occurred_at DESC LIMIT ?""",
        (workspace_id, max(1, min(limit, 200))),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["actor_name"] = _event_actor(conn, item)
        result.append(item)
    return result


def get_context_manifest(conn: Database, *, workspace_id: str, manifest_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM context_manifests WHERE id = ? AND workspace_id = ?",
        (manifest_id, workspace_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("focus_json", "selected_event_ids_json", "selected_memory_ids_json"):
        target = key.removesuffix("_json")
        try:
            result[target] = json.loads(row[key] or "[]")
        except (TypeError, ValueError):
            result[target] = []
    return result


def get_run_context_summary(conn: Database, *, workspace_id: str, run_id: str) -> dict:
    row = conn.execute(
        """SELECT id FROM context_manifests
        WHERE workspace_id = ? AND run_id = ?
        ORDER BY created_at DESC LIMIT 1""",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return {
            "context_manifest_id": None,
            "event_ids": [],
            "memory_ids": [],
        }
    manifest = get_context_manifest(
        conn, workspace_id=workspace_id, manifest_id=row["id"]
    ) or {}
    return {
        "context_manifest_id": manifest.get("id"),
        "event_ids": manifest.get("selected_event_ids", []),
        "memory_ids": manifest.get("selected_memory_ids", []),
    }


def rebuild_workspace_events(conn: Database, workspace_id: str) -> dict:
    """Backfill legacy rows into company_events without duplicating them."""
    counts = {"messages": 0, "tasks": 0, "outputs": 0, "knowledge": 0}
    for row in conn.execute(
        """SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE c.workspace_id = ? ORDER BY m.created_at""", (workspace_id,)
    ).fetchall():
        record_message_event(conn, row)
        counts["messages"] += 1
    for row in conn.execute("SELECT * FROM task_events WHERE workspace_id = ?", (workspace_id,)).fetchall():
        record_task_event_projection(conn, row)
        counts["tasks"] += 1
    for row in conn.execute("SELECT * FROM task_outputs WHERE workspace_id = ?", (workspace_id,)).fetchall():
        record_task_event_projection(conn, row, event_type="task_output")
        counts["outputs"] += 1
    for row in conn.execute("SELECT * FROM knowledge_sources WHERE workspace_id = ?", (workspace_id,)).fetchall():
        record_company_event(
            conn, workspace_id=workspace_id, event_type="knowledge", source_id=row["id"],
            title=row["title"], content=row["content"], occurred_at=row["updated_at"],
            importance=3.0, metadata={"category": row["category"]},
        )
        counts["knowledge"] += 1
    return counts


def send_internal_ping(
    conn: Database,
    *,
    workspace_id: str,
    from_agent_id: str,
    to_agent_id: str,
    content: str,
    run_id: str | None = None,
) -> dict:
    """Create a durable employee-to-employee DM with basic loop protection."""
    if from_agent_id == to_agent_id:
        raise ValueError("员工不能给自己发消息")
    members = conn.execute(
        """SELECT id, name FROM agents WHERE workspace_id = ? AND id IN (?, ?)""",
        (workspace_id, from_agent_id, to_agent_id),
    ).fetchall()
    if len(members) != 2:
        raise ValueError("同事不存在或不属于当前公司")
    recent = conn.execute(
        """SELECT m.sender_id, m.content FROM messages m
        JOIN conversations c ON c.id = m.conversation_id AND c.kind = 'dm'
        WHERE m.sender_type = 'agent'
          AND EXISTS (SELECT 1 FROM conversation_members pair
                      WHERE pair.conversation_id = m.conversation_id
                        AND pair.agent_id = ?)
          AND EXISTS (SELECT 1 FROM conversation_members pair
                      WHERE pair.conversation_id = m.conversation_id
                        AND pair.agent_id = ?)
        ORDER BY m.created_at DESC LIMIT 3""",
        (from_agent_id, to_agent_id),
    ).fetchall()
    recent_turns = conn.execute(
        """SELECT m.sender_id FROM messages m
        JOIN conversations c ON c.id = m.conversation_id AND c.kind = 'dm'
        WHERE m.sender_type = 'agent'
          AND EXISTS (SELECT 1 FROM conversation_members pair
                      WHERE pair.conversation_id = m.conversation_id
                        AND pair.agent_id = ?)
          AND EXISTS (SELECT 1 FROM conversation_members pair
                      WHERE pair.conversation_id = m.conversation_id
                        AND pair.agent_id = ?)
        ORDER BY m.created_at DESC LIMIT 12""",
        (from_agent_id, to_agent_id),
    ).fetchall()
    if len(recent_turns) >= 12:
        raise ValueError("内部协作已达到连续自动轮次上限，请转成任务或等待人类补充")
    normalized = " ".join(content.split()).lower()
    if not normalized:
        raise ValueError("消息不能为空")
    if any(" ".join(row["content"].split()).lower() == normalized for row in recent):
        raise ValueError("检测到重复 ping，请先产生新的事实或任务")
    from app.services.workspace import add_message, new_id, now_iso

    # Keep colleague DMs separate from the employee's owner-facing DM. The
    # existing conversations table permits a NULL agent_id for this shared
    # two-person inbox, while conversation_members carries both participants.
    conversation = conn.execute(
        """SELECT c.* FROM conversations c
        WHERE c.workspace_id = ? AND c.kind = 'dm' AND c.agent_id IS NULL
          AND EXISTS (SELECT 1 FROM conversation_members cm
                      WHERE cm.conversation_id = c.id AND cm.agent_id = ?)
          AND EXISTS (SELECT 1 FROM conversation_members cm
                      WHERE cm.conversation_id = c.id AND cm.agent_id = ?)
        ORDER BY c.created_at LIMIT 1""",
        (workspace_id, from_agent_id, to_agent_id),
    ).fetchone()
    if conversation is None:
        conversation_id = new_id("conv")
        timestamp = now_iso()
        conn.execute(
            """INSERT INTO conversations (
              id, workspace_id, kind, name, agent_id, unread, created_at, updated_at
            ) VALUES (?, ?, 'dm', ?, NULL, 0, ?, ?)""",
            (conversation_id, workspace_id, "同事协作", timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)" ,
            (conversation_id, from_agent_id),
        )
        conn.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
            (conversation_id, to_agent_id),
        )
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    message = add_message(
        conn,
        conversation_id=conversation["id"],
        sender_type="agent",
        sender_id=from_agent_id,
        content=content,
    )
    source_task_id = None
    if run_id:
        run = conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        source_task_id = run["task_id"] if run else None
    from app.services.workforce import create_work_request

    work_request = create_work_request(
        conn,
        workspace_id=workspace_id,
        requester_type="agent",
        requester_id=from_agent_id,
        target_agent_id=to_agent_id,
        content=content,
        conversation_id=conversation["id"],
        source_message_id=message["id"],
        source_task_id=source_task_id,
    )
    if run_id:
        record_company_event(
            conn, workspace_id=workspace_id, event_type="ping", source_id=run_id,
            title="同事间发起协作", content=content, conversation_id=conversation["id"],
            actor_agent_id=from_agent_id, metadata={"to_agent_id": to_agent_id},
        )
    return {
        "conversation_id": conversation["id"],
        "message_id": message["id"],
        "work_request_id": work_request["id"],
        "to_agent_id": to_agent_id,
    }
