from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class StateStore:
    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path

    async def init(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    target_chat_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(source_chat_id, source_message_id, target_chat_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS joined_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL UNIQUE,
                    link TEXT,
                    joined_at TEXT NOT NULL,
                    account_label TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_state (
                    name TEXT PRIMARY KEY,
                    cycles_completed INTEGER NOT NULL DEFAULT 0,
                    total_sent INTEGER NOT NULL DEFAULT 0,
                    total_failed INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT
                )
                """
            )
            await db.commit()

    async def already_sent(self, source_chat_id: int, source_message_id: int, target_chat_id: int) -> bool:
        async with aiosqlite.connect(self.sqlite_path) as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM sent_messages
                WHERE source_chat_id = ? AND source_message_id = ? AND target_chat_id = ?
                LIMIT 1
                """,
                (source_chat_id, source_message_id, target_chat_id),
            )
            row = await cursor.fetchone()
            return row is not None

    async def mark_sent(self, source_chat_id: int, source_message_id: int, target_chat_id: int) -> None:
        sent_at = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO sent_messages (
                    source_chat_id, source_message_id, target_chat_id, sent_at
                ) VALUES (?, ?, ?, ?)
                """,
                (source_chat_id, source_message_id, target_chat_id, sent_at),
            )
            await db.commit()

    async def mark_joined(self, chat_id: int, link: str, account_label: str = "") -> None:
        joined_at = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO joined_groups (chat_id, link, joined_at, account_label)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, link, joined_at, account_label),
            )
            await db.commit()

    async def is_joined(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.sqlite_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM joined_groups WHERE chat_id = ? LIMIT 1",
                (chat_id,),
            )
            row = await cursor.fetchone()
            return row is not None

    async def list_joined_ids(self) -> list[int]:
        async with aiosqlite.connect(self.sqlite_path) as db:
            cursor = await db.execute("SELECT chat_id FROM joined_groups ORDER BY joined_at DESC")
            rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]

    async def save_campaign_state(self, name: str, cycles: int, total_sent: int, total_failed: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO campaign_state (name, cycles_completed, total_sent, total_failed, last_run_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, cycles, total_sent, total_failed, now),
            )
            await db.commit()

    async def reset_sent_for_campaign(self, source_chat_id: int, source_message_id: int) -> None:
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                "DELETE FROM sent_messages WHERE source_chat_id = ? AND source_message_id = ?",
                (source_chat_id, source_message_id),
            )
            await db.commit()

    async def clear_all(self) -> None:
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute("DELETE FROM sent_messages")
            await db.execute("DELETE FROM joined_groups")
            await db.execute("DELETE FROM campaign_state")
            await db.commit()

    async def get_recent_sent(self, limit: int = 200) -> list[dict]:
        async with aiosqlite.connect(self.sqlite_path) as db:
            cursor = await db.execute(
                """
                SELECT source_chat_id, source_message_id, target_chat_id, sent_at
                FROM sent_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "source_chat_id": r[0],
                    "source_message_id": r[1],
                    "target_chat_id": r[2],
                    "sent_at": r[3],
                }
                for r in rows
            ]
