"""SQLite storage. One short-lived aiosqlite connection per operation (simple and safe)."""
import json
import os
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await db.commit()


async def ping(path: str) -> bool:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("SELECT 1")
        row = await cur.fetchone()
        return row is not None and row[0] == 1


async def get_session_prompt(path: str, session_id: str):
    """Returns the stored custom system prompt, or None."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_history(path: str, session_id: str) -> list:
    """Oldest first. Only role + content (what the LLM needs)."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
        )
        rows = await cur.fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]


async def get_messages_for_api(path: str, session_id: str):
    """Returns None if the session has no stored messages (-> 404)."""
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    out = []
    for role, content, meta in rows:
        item = {"role": role, "content": content}
        if role == "assistant" and meta:
            item["metadata"] = json.loads(meta)
        out.append(item)
    return out


async def save_exchange(path: str, session_id: str, system_prompt, user_text: str, reply: str, metadata: dict) -> None:
    """Writes the session row + user row + assistant row in ONE transaction,
    and only ever called after the LLM call succeeded."""
    now = _now()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO sessions(session_id, system_prompt, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET system_prompt = COALESCE(excluded.system_prompt, sessions.system_prompt)",
            (session_id, system_prompt, now),
        )
        await db.execute(
            "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES (?, 'user', ?, NULL, ?)",
            (session_id, user_text, now),
        )
        await db.execute(
            "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, reply, json.dumps(metadata), _now()),
        )
        await db.commit()
