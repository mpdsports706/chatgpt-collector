"""SQLite staging for raw ChatGPT web API payloads."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DEFER_412_HARD_CAP, DEFER_412_MAX_ATTEMPTS, DEFER_412_MIN_DAYS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


@dataclass(frozen=True)
class ReconciliationSummary:
    list_total: int | None
    list_total_at: str | None
    staged: int
    deferred_total: int
    deferred_retryable: int
    deferred_permanent: int
    missing_estimate: int | None
    is_complete: bool


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
                    list_update_time REAL,
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
                CREATE TABLE IF NOT EXISTS deferred_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_error TEXT
                );
                """
            )
            cols = {
                row["name"]
                for row in con.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "list_update_time" not in cols:
                con.execute(
                    "ALTER TABLE conversations ADD COLUMN list_update_time REAL DEFAULT 0"
                )
            legacy = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='skipped_conversations'"
            ).fetchone()
            if legacy:
                rows = con.execute(
                    "SELECT conversation_id, reason, skipped_at FROM skipped_conversations"
                ).fetchall()
                for row in rows:
                    con.execute(
                        """
                        INSERT INTO deferred_conversations(
                            conversation_id, reason, attempt_count,
                            first_seen_at, last_attempt_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(conversation_id) DO NOTHING
                        """,
                        (
                            row["conversation_id"],
                            row["reason"],
                            DEFER_412_MAX_ATTEMPTS,
                            row["skipped_at"],
                            row["skipped_at"],
                            "migrated from skipped_conversations",
                        ),
                    )
                con.execute("DROP TABLE skipped_conversations")

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

    def upsert_conversation(
        self,
        conv: dict[str, Any],
        *,
        list_update_time: float | None = None,
    ) -> bool:
        """Insert or update when payload changed. Returns True if stored/updated."""
        conv_id = str(conv.get("conversation_id") or conv.get("id") or "")
        if not conv_id:
            return False
        raw = json.dumps(conv, ensure_ascii=False, separators=(",", ":"))
        digest = _content_hash(raw)
        title = conv.get("title")
        create_time = _as_epoch(conv.get("create_time"))
        update_time = _as_epoch(conv.get("update_time") or conv.get("create_time"))
        list_ut = list_update_time if list_update_time is not None else update_time
        now = _utc_now()
        with self._conn() as con:
            row = con.execute(
                "SELECT content_hash FROM conversations WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if row and row["content_hash"] == digest:
                con.execute(
                    """
                    UPDATE conversations
                    SET list_update_time = MAX(COALESCE(list_update_time, 0), ?)
                    WHERE conversation_id = ?
                    """,
                    (list_ut, conv_id),
                )
                return False
            con.execute(
                """
                INSERT INTO conversations(
                    conversation_id, title, create_time, update_time, list_update_time,
                    raw_json, content_hash, fetched_at, exported_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT exported_hash FROM conversations WHERE conversation_id = ?), NULL
                ))
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    create_time = excluded.create_time,
                    update_time = excluded.update_time,
                    list_update_time = excluded.list_update_time,
                    raw_json = excluded.raw_json,
                    content_hash = excluded.content_hash,
                    fetched_at = excluded.fetched_at
                """,
                (
                    conv_id,
                    title,
                    create_time,
                    update_time,
                    list_ut,
                    raw,
                    digest,
                    now,
                    conv_id,
                ),
            )
        self.clear_deferred(conv_id)
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

    def conversation_meta(self, conversation_id: str) -> tuple[float, float] | None:
        """Return (detail update_time, list_update_time) for skip decisions."""
        with self._conn() as con:
            row = con.execute(
                """
                SELECT update_time, list_update_time
                FROM conversations WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            return float(row["update_time"] or 0), float(row["list_update_time"] or 0)

    def should_skip_summary(self, conversation_id: str, summary_update_time: float) -> bool:
        """Skip fetch only when both detail and list timestamps cover the summary."""
        if not conversation_id or not summary_update_time:
            return False
        meta = self.conversation_meta(conversation_id)
        if not meta:
            return False
        detail_ut, list_ut = meta
        return detail_ut >= summary_update_time and list_ut >= summary_update_time

    def _deferred_row(self, conversation_id: str) -> sqlite3.Row | None:
        with self._conn() as con:
            return con.execute(
                "SELECT * FROM deferred_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()

    def is_permanently_deferred(self, conversation_id: str) -> bool:
        row = self._deferred_row(conversation_id)
        if not row:
            return False
        attempts = int(row["attempt_count"])
        if attempts >= DEFER_412_HARD_CAP:
            return True
        if attempts < DEFER_412_MAX_ATTEMPTS:
            return False
        first = _parse_utc(str(row["first_seen_at"]))
        last = _parse_utc(str(row["last_attempt_at"]))
        span = last - first
        return span >= timedelta(days=DEFER_412_MIN_DAYS)

    def should_skip_fetch(self, conversation_id: str, *, retry_412: bool = False) -> bool:
        if not conversation_id:
            return False
        if retry_412:
            return False
        return self.is_permanently_deferred(conversation_id)

    def record_deferred_412(self, conversation_id: str, error: str) -> None:
        now = _utc_now()
        with self._conn() as con:
            row = con.execute(
                "SELECT attempt_count FROM deferred_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            con.execute(
                """
                INSERT INTO deferred_conversations(
                    conversation_id, reason, attempt_count,
                    first_seen_at, last_attempt_at, last_error
                ) VALUES (?, 'stale_412', 1, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    attempt_count = attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    last_error = excluded.last_error
                """,
                (conversation_id, now, now, error[:500]),
            )

    def clear_deferred(self, conversation_id: str) -> None:
        with self._conn() as con:
            con.execute(
                "DELETE FROM deferred_conversations WHERE conversation_id = ?",
                (conversation_id,),
            )

    def reset_deferred_412(self, *, include_permanent: bool = False) -> int:
        with self._conn() as con:
            if include_permanent:
                cur = con.execute("DELETE FROM deferred_conversations WHERE reason = 'stale_412'")
                return int(cur.rowcount)
            rows = con.execute(
                "SELECT conversation_id FROM deferred_conversations WHERE reason = 'stale_412'"
            ).fetchall()
        removed = 0
        for row in rows:
            conv_id = str(row["conversation_id"])
            if not self.is_permanently_deferred(conv_id):
                with self._conn() as con:
                    con.execute(
                        "DELETE FROM deferred_conversations WHERE conversation_id = ?",
                        (conv_id,),
                    )
                removed += 1
        return removed

    def deferred_stats(self) -> tuple[int, int, int]:
        """Return (total, retryable, permanent)."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT conversation_id FROM deferred_conversations WHERE reason = 'stale_412'"
            ).fetchall()
        total = len(rows)
        permanent = sum(1 for row in rows if self.is_permanently_deferred(row["conversation_id"]))
        return total, total - permanent, permanent

    def skipped_count(self) -> int:
        """Backward-compatible alias for permanent deferred count."""
        return self.deferred_stats()[2]

    def reconciliation_summary(self) -> ReconciliationSummary:
        raw_total = self.get_state("list_total")
        list_total = int(raw_total) if raw_total and raw_total.isdigit() else None
        list_total_at = self.get_state("list_total_at")
        staged = self.count()
        deferred_total, deferred_retryable, deferred_permanent = self.deferred_stats()
        missing: int | None = None
        is_complete = False
        if list_total is not None:
            missing = max(0, list_total - staged - deferred_permanent)
            is_complete = missing == 0 and deferred_retryable == 0
        return ReconciliationSummary(
            list_total=list_total,
            list_total_at=list_total_at,
            staged=staged,
            deferred_total=deferred_total,
            deferred_retryable=deferred_retryable,
            deferred_permanent=deferred_permanent,
            missing_estimate=missing,
            is_complete=is_complete,
        )
