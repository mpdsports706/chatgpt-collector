"""SQLite staging for raw ChatGPT web API payloads."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class StagingStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._conn() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    create_time REAL,
                    update_time REAL,
                    raw_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    exported_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_update
                    ON conversations(update_time DESC);
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skipped_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    skipped_at TEXT NOT NULL
                );
                """
            )

    def get_state(self, key: str) -> str | None:
        with self._conn() as con:
            row = con.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO sync_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def upsert_conversation(self, conv: dict[str, Any]) -> bool:
        """Insert or update when payload changed. Returns True if stored/updated."""
        conv_id = str(conv.get("conversation_id") or conv.get("id") or "")
        if not conv_id:
            return False
        raw = json.dumps(conv, ensure_ascii=False, separators=(",", ":"))
        digest = _content_hash(raw)
        title = conv.get("title")
        create_time = _as_epoch(conv.get("create_time"))
        update_time = _as_epoch(conv.get("update_time") or conv.get("create_time"))
        now = _utc_now()
        with self._conn() as con:
            row = con.execute(
                "SELECT content_hash FROM conversations WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if row and row["content_hash"] == digest:
                return False
            con.execute(
                """
                INSERT INTO conversations(
                    conversation_id, title, create_time, update_time,
                    raw_json, content_hash, fetched_at, exported_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT exported_hash FROM conversations WHERE conversation_id = ?), NULL
                ))
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    create_time = excluded.create_time,
                    update_time = excluded.update_time,
                    raw_json = excluded.raw_json,
                    content_hash = excluded.content_hash,
                    fetched_at = excluded.fetched_at
                """,
                (conv_id, title, create_time, update_time, raw, digest, now, conv_id),
            )
        return True

    def list_conversations(self, *, changed_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM conversations"
        if changed_only:
            sql += " WHERE exported_hash IS NULL OR exported_hash != content_hash"
        sql += " ORDER BY update_time DESC, conversation_id"
        with self._conn() as con:
            return list(con.execute(sql))

    def mark_exported(self, conversation_id: str, content_hash: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE conversations SET exported_hash = ? WHERE conversation_id = ?",
                (content_hash, conversation_id),
            )

    def count(self) -> int:
        with self._conn() as con:
            row = con.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()
            return int(row["c"]) if row else 0

    def conversation_meta(self, conversation_id: str) -> tuple[float, str] | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT update_time, content_hash FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            return float(row["update_time"] or 0), str(row["content_hash"])

    def is_skipped(self, conversation_id: str) -> bool:
        with self._conn() as con:
            row = con.execute(
                "SELECT 1 FROM skipped_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return row is not None

    def mark_skipped(self, conversation_id: str, reason: str) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO skipped_conversations(conversation_id, reason, skipped_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    reason = excluded.reason,
                    skipped_at = excluded.skipped_at
                """,
                (conversation_id, reason, _utc_now()),
            )

    def skipped_count(self) -> int:
        with self._conn() as con:
            row = con.execute("SELECT COUNT(*) AS c FROM skipped_conversations").fetchone()
            return int(row["c"]) if row else 0
