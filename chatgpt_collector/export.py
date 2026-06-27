"""Export staged conversations to hub-ready JSON files."""
from __future__ import annotations

from dataclasses import dataclass

from .config import hub_export_dir
from .staging import StagingStore


@dataclass
class ExportStats:
    exported: int = 0
    pending: int = 0
    total: int = 0


def export_changed(store: StagingStore) -> ExportStats:
    out_dir = hub_export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = ExportStats(total=store.count())
    for row in store.list_conversations(changed_only=True):
        conv_id = str(row["conversation_id"])
        dest = out_dir / f"{conv_id}.chatgpt_web.json"
        dest.write_text(str(row["raw_json"]), encoding="utf-8")
        store.mark_exported(conv_id, str(row["content_hash"]))
        stats.exported += 1
    stats.pending = len(store.list_conversations(changed_only=True))
    return stats
