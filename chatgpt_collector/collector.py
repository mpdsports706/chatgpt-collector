"""Backfill and incremental collection orchestration."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .config import DEFAULT_PAGE_SIZE
from .staging import StagingStore

from .client import ChatGPTApiError, StaleConversationError

logger = logging.getLogger(__name__)


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


def _error_key(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    name = type(exc).__name__
    msg = str(exc).split("\n", 1)[0][:100]
    return f"{name}{f'({status})' if status else ''}: {msg}"


@dataclass
class CollectStats:
    listed: int = 0
    fetched: int = 0
    stored: int = 0
    skipped: int = 0
    stale_skipped: int = 0
    errors: int = 0
    error_reasons: Counter[str] = field(default_factory=Counter)


def _is_stale(exc: BaseException) -> bool:
    if isinstance(exc, StaleConversationError):
        return True
    return isinstance(exc, ChatGPTApiError) and exc.status == 412


def _handle_fetch_error(
    store: StagingStore,
    stats: CollectStats,
    conv_id: str,
    exc: BaseException,
    *,
    label: str,
) -> None:
    if _is_stale(exc):
        if conv_id:
            store.mark_skipped(conv_id, "stale_412")
        stats.stale_skipped += 1
        return
    stats.errors += 1
    stats.error_reasons[_error_key(exc)] += 1
    if stats.errors <= 3:
        logger.warning("%s error for %s: %s", label, conv_id, exc)


async def backfill(
    store: StagingStore,
    client: ChatGPTClient,
    *,
    max_conversations: int | None = None,
    since_update_time: float | None = None,
) -> CollectStats:
    stats = CollectStats()
    async for summary in client.iter_conversation_summaries(page_size=DEFAULT_PAGE_SIZE):
        stats.listed += 1
        update_time = _as_epoch(summary.get("update_time") or summary.get("create_time"))
        if since_update_time is not None and update_time and update_time < since_update_time:
            stats.skipped += 1
            continue
        conv_id = str(summary.get("id") or summary.get("conversation_id") or "")
        if conv_id and store.is_skipped(conv_id):
            stats.skipped += 1
            continue
        existing = store.conversation_meta(conv_id) if conv_id else None
        if existing and update_time and existing[0] >= update_time:
            stats.skipped += 1
            continue
        try:
            detail = await client.fetch_full_conversation(summary)
            stats.fetched += 1
            if store.upsert_conversation(detail):
                stats.stored += 1
            else:
                stats.skipped += 1
        except Exception as exc:
            _handle_fetch_error(store, stats, conv_id, exc, label="backfill")
        if max_conversations is not None and stats.fetched >= max_conversations:
            break
    store.set_state("last_backfill_listed", str(stats.listed))
    store.set_state("last_backfill_stored", str(stats.stored))
    return stats


async def watch(store: StagingStore, client: ChatGPTClient, *, lookback: int = 50) -> CollectStats:
    """Fetch recent conversations; stop when we reach unchanged rows."""
    stats = CollectStats()
    seen_unchanged = 0
    async for summary in client.iter_conversation_summaries(page_size=min(DEFAULT_PAGE_SIZE, lookback)):
        stats.listed += 1
        if stats.listed > lookback:
            break
        conv_id = str(summary.get("id") or summary.get("conversation_id") or "")
        update_time = _as_epoch(summary.get("update_time"))
        existing = store.conversation_meta(conv_id)
        if existing and update_time and existing[0] >= update_time:
            seen_unchanged += 1
            if seen_unchanged >= 5:
                break
            stats.skipped += 1
            continue
        try:
            detail = await client.fetch_full_conversation(summary)
            stats.fetched += 1
            if store.upsert_conversation(detail):
                stats.stored += 1
            else:
                stats.skipped += 1
                seen_unchanged += 1
        except Exception as exc:
            _handle_fetch_error(store, stats, conv_id, exc, label="watch")
    store.set_state("last_watch_stored", str(stats.stored))
    return stats
