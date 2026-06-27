"""Staging store unit tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chatgpt_collector.config import DEFER_412_MAX_ATTEMPTS
from chatgpt_collector.staging import StagingStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chatgpt_web_sample.chatgpt_web.json"


@pytest.fixture
def conv() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_staging_upsert_and_export_tracking(tmp_path, conv):
    store = StagingStore(tmp_path / "staging.sqlite")
    assert store.upsert_conversation(conv, list_update_time=1718880060.0) is True
    assert store.upsert_conversation(conv, list_update_time=1718880060.0) is False
    assert store.count() == 1
    rows = store.list_conversations(changed_only=True)
    assert len(rows) == 1
    store.mark_exported(conv["conversation_id"], rows[0]["content_hash"])
    assert store.list_conversations(changed_only=True) == []


def test_should_skip_summary_requires_both_timestamps(tmp_path, conv):
    store = StagingStore(tmp_path / "staging.sqlite")
    conv_id = conv["conversation_id"]
    store.upsert_conversation(conv, list_update_time=100.0)
    assert store.should_skip_summary(conv_id, 100.0) is True
    assert store.should_skip_summary(conv_id, 101.0) is False


def test_deferred_412_not_permanent_on_first_attempt(tmp_path):
    store = StagingStore(tmp_path / "staging.sqlite")
    store.record_deferred_412("conv-a", "412 stale")
    assert store.is_permanently_deferred("conv-a") is False
    assert store.should_skip_fetch("conv-a") is False


def test_deferred_412_becomes_permanent_after_max_attempts_and_span(tmp_path):
    store = StagingStore(tmp_path / "staging.sqlite")
    conv_id = "conv-b"
    first = datetime.now(timezone.utc) - timedelta(days=8)
    with store._conn() as con:
        con.execute(
            """
            INSERT INTO deferred_conversations(
                conversation_id, reason, attempt_count,
                first_seen_at, last_attempt_at, last_error
            ) VALUES (?, 'stale_412', ?, ?, ?, ?)
            """,
            (
                conv_id,
                DEFER_412_MAX_ATTEMPTS,
                first.isoformat(),
                datetime.now(timezone.utc).isoformat(),
                "412",
            ),
        )
    assert store.is_permanently_deferred(conv_id) is True
    assert store.should_skip_fetch(conv_id) is True


def test_successful_fetch_clears_deferred(tmp_path, conv):
    store = StagingStore(tmp_path / "staging.sqlite")
    conv_id = conv["conversation_id"]
    store.record_deferred_412(conv_id, "412")
    store.upsert_conversation(conv)
    assert store.deferred_stats()[0] == 0


def test_reconciliation_summary(tmp_path, conv):
    store = StagingStore(tmp_path / "staging.sqlite")
    store.set_state("list_total", "10")
    store.set_state("list_total_at", "2026-06-27T00:00:00+00:00")
    store.upsert_conversation(conv)
    recon = store.reconciliation_summary()
    assert recon.list_total == 10
    assert recon.staged == 1
    assert recon.missing_estimate == 9
    assert recon.is_complete is False
