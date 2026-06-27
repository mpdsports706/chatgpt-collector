"""Staging store unit tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatgpt_collector.staging import StagingStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chatgpt_web_sample.chatgpt_web.json"


@pytest.fixture
def conv() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_staging_upsert_and_export_tracking(tmp_path, conv):
    store = StagingStore(tmp_path / "staging.sqlite")
    assert store.upsert_conversation(conv) is True
    assert store.upsert_conversation(conv) is False
    assert store.count() == 1
    rows = store.list_conversations(changed_only=True)
    assert len(rows) == 1
    store.mark_exported(conv["conversation_id"], rows[0]["content_hash"])
    assert store.list_conversations(changed_only=True) == []
