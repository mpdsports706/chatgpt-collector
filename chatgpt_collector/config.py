"""Paths and defaults for the ChatGPT web collector."""
from __future__ import annotations

import os
from pathlib import Path


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def state_dir() -> Path:
    raw = os.environ.get("CHATGPT_COLLECTOR_STATE_DIR", "~/.ai-telemetry-hub/chatgpt-collector")
    return _expand(raw)


def storage_state_path() -> Path:
    return state_dir() / "storage_state.json"


def browser_profile_dir() -> Path:
    raw = os.environ.get("CHATGPT_COLLECTOR_PROFILE_DIR", str(state_dir() / "browser_profile"))
    path = _expand(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_db_path() -> Path:
    return state_dir() / "staging.sqlite"


def hub_export_dir() -> Path:
    raw = os.environ.get(
        "CHATGPT_WEB_STAGING_DIR",
        str(state_dir() / "hub"),
    )
    return _expand(raw)


def chatgpt_base_urls() -> list[str]:
    raw = os.environ.get("CHATGPT_BASE_URL", "https://chatgpt.com,https://chat.openai.com")
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def hub_api_url() -> str:
    return os.environ.get("AI_TELEMETRY_API", "http://localhost:8000").rstrip("/")


DEFAULT_PAGE_SIZE = int(os.environ.get("CHATGPT_COLLECTOR_PAGE_SIZE", "28"))
REQUEST_DELAY_SEC = float(os.environ.get("CHATGPT_COLLECTOR_DELAY_SEC", "1.25"))
RATE_LIMIT_MAX_RETRIES = int(os.environ.get("CHATGPT_COLLECTOR_RATE_RETRIES", "6"))
LOGIN_TIMEOUT_MINUTES = int(os.environ.get("CHATGPT_LOGIN_TIMEOUT_MINUTES", "30"))
DEFER_412_MAX_ATTEMPTS = int(os.environ.get("CHATGPT_COLLECTOR_412_MAX_ATTEMPTS", "3"))
DEFER_412_MIN_DAYS = int(os.environ.get("CHATGPT_COLLECTOR_412_MIN_DAYS", "7"))
DEFER_412_HARD_CAP = int(os.environ.get("CHATGPT_COLLECTOR_412_HARD_CAP", "5"))


def collection_headless() -> bool:
    """Default False — use headed browser until collection is proven."""
    raw = os.environ.get("CHATGPT_COLLECTOR_HEADLESS", "false").lower()
    return raw in ("1", "true", "yes")


def collection_use_profile() -> bool:
    """Default False — reuse login profile only when explicitly enabled (can crash on macOS)."""
    raw = os.environ.get("CHATGPT_COLLECTOR_USE_PROFILE", "false").lower()
    return raw in ("1", "true", "yes")
