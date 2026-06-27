"""Push hub exports into the telemetry API."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import hub_api_url, hub_export_dir
from .export import ExportStats, export_changed
from .staging import StagingStore


@dataclass
class SyncStats:
    export: ExportStats
    import_conversations: int = 0
    import_messages: int = 0
    import_errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.import_errors is None:
            self.import_errors = []


def trigger_hub_import(*, api_url: str | None = None, hub_path: str | None = None) -> dict:
    base = (api_url or hub_api_url()).rstrip("/")
    path = hub_path or "/data/chatgpt_web"
    payload = json.dumps({"path": path, "connector": "chatgpt_web"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/imports/path",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_to_hub(store: StagingStore, *, api_url: str | None = None) -> SyncStats:
    export_stats = export_changed(store)
    stats = SyncStats(export=export_stats)
    if export_stats.exported == 0 and store.count() == 0:
        stats.import_errors.append("staging is empty; run backfill first")
        return stats
    try:
        result = trigger_hub_import(api_url=api_url)
        stats.import_conversations = int(result.get("conversations_imported") or 0)
        stats.import_messages = int(result.get("messages_imported") or 0)
        if result.get("errors"):
            stats.import_errors.extend(str(e) for e in result["errors"])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        stats.import_errors.append(f"HTTP {exc.code}: {body[:500]}")
    except Exception as exc:  # pragma: no cover - network
        stats.import_errors.append(f"{type(exc).__name__}: {exc}")
    return stats
