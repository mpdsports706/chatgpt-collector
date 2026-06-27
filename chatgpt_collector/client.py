"""ChatGPT backend-api client via authenticated Playwright request context."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from playwright.async_api import APIResponse, Request

from .browser import AuthError, ChatGPTBrowser
from .config import (
    DEFAULT_PAGE_SIZE,
    RATE_LIMIT_MAX_RETRIES,
    REQUEST_DELAY_SEC,
    chatgpt_base_urls,
)

_LIST_QUERY = "offset={offset}&limit={limit}&order=updated&is_archived=false&is_starred=false"


class ChatGPTApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class StaleConversationError(ChatGPTApiError):
    """ChatGPT returned 412 conversation_precondition_failed — thread not fetchable via API."""


async def _read_json(response: APIResponse) -> Any:
    text = await response.text()
    if response.status >= 400:
        raise ChatGPTApiError(
            f"ChatGPT API {response.status}: {text[:300]}",
            status=response.status,
            url=response.url,
        )
    if not text.strip():
        return {}
    return json.loads(text)


class ChatGPTClient:
    def __init__(self, browser: ChatGPTBrowser) -> None:
        self._browser = browser
        self._api_base: str | None = None
        self._auth_headers: dict[str, str] = {}
        self._warmed = False
        self.last_list_total: int = 0

    async def ensure_warmed_up(self) -> None:
        """Load ChatGPT UI and capture Bearer token from the app's own API calls."""
        if self._warmed and self._auth_headers:
            return

        page = self._browser.page
        ready = asyncio.Event()

        async def on_request(request: Request) -> None:
            if "backend-api/" not in request.url:
                return
            auth = request.headers.get("authorization") or ""
            if not auth.startswith("Bearer "):
                return
            self._auth_headers = {
                "authorization": auth,
                "referer": "https://chatgpt.com/",
            }
            for key in ("oai-device-id", "oai-language"):
                value = request.headers.get(key)
                if value:
                    self._auth_headers[key] = value
            ready.set()

        page.on("request", on_request)
        try:
            loaded = False
            for base in chatgpt_base_urls():
                try:
                    await page.goto(base, wait_until="domcontentloaded", timeout=120_000)
                    loaded = True
                    break
                except Exception:
                    continue
            if not loaded:
                raise AuthError("Could not load ChatGPT in the browser.")

            try:
                await asyncio.wait_for(ready.wait(), timeout=90)
            except asyncio.TimeoutError as exc:
                raise AuthError(
                    "ChatGPT did not issue an API token within 90s. "
                    "If the browser opened logged out, run: make chatgpt-login"
                ) from exc
        finally:
            page.remove_listener("request", on_request)

        self._warmed = True

    async def reload_session(self) -> None:
        """Refresh page + Bearer token (ChatGPT asks for this on stale 412s)."""
        self._warmed = False
        self._auth_headers.clear()
        await self.ensure_warmed_up()

    async def _api_get(self, url: str) -> APIResponse:
        await self.ensure_warmed_up()
        last_resp: APIResponse | None = None
        for attempt in range(RATE_LIMIT_MAX_RETRIES):
            resp = await self._browser.context.request.get(url, headers=self._auth_headers)
            last_resp = resp
            if resp.status == 429:
                wait = min(60.0, 3.0 * (2**attempt))
                await asyncio.sleep(wait)
                continue
            if resp.status in (401, 403) and attempt == 0:
                self._warmed = False
                self._auth_headers.clear()
                await self.ensure_warmed_up()
                continue
            return resp
        return last_resp  # type: ignore[return-value]

    async def _resolve_api_base(self) -> str:
        if self._api_base:
            return self._api_base
        await self.ensure_warmed_up()
        for base in chatgpt_base_urls():
            probe = f"{base}/backend-api/conversations?{_LIST_QUERY.format(offset=0, limit=1)}"
            resp = await self._api_get(probe)
            if resp.status == 200:
                self._api_base = base
                return base
            if resp.status in (401, 403):
                raise AuthError(
                    "ChatGPT session expired or unauthorized. Re-run: make chatgpt-login"
                )
        raise ChatGPTApiError("Could not reach ChatGPT backend-api on any configured base URL")

    async def list_conversations(self, *, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        base = await self._resolve_api_base()
        url = f"{base}/backend-api/conversations?{_LIST_QUERY.format(offset=offset, limit=limit)}"
        resp = await self._api_get(url)
        return await _read_json(resp)

    async def _open_conversation_in_browser(self, conversation_id: str) -> None:
        """Load a thread in the UI — sometimes required before the API will serve it."""
        page = self._browser.page
        for base in chatgpt_base_urls():
            try:
                await page.goto(
                    f"{base}/c/{conversation_id}",
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
                await asyncio.sleep(1.5)
                return
            except Exception:
                continue

    async def get_conversation(
        self,
        conversation_id: str,
        *,
        _open_retry: bool = True,
        _reload_retry: bool = True,
    ) -> dict[str, Any]:
        base = await self._resolve_api_base()
        last_error: Exception | None = None
        for path in (
            f"/backend-api/conversation/{conversation_id}",
            f"/backend-api/conversations/{conversation_id}",
        ):
            url = f"{base}{path}"
            resp = await self._api_get(url)
            if resp.status == 412:
                if _open_retry:
                    await self._open_conversation_in_browser(conversation_id)
                    return await self.get_conversation(
                        conversation_id,
                        _open_retry=False,
                        _reload_retry=_reload_retry,
                    )
                if _reload_retry:
                    await self.reload_session()
                    return await self.get_conversation(
                        conversation_id,
                        _open_retry=False,
                        _reload_retry=False,
                    )
                text = await resp.text()
                raise StaleConversationError(
                    f"ChatGPT API 412: {text[:300]}",
                    status=412,
                    url=url,
                )
            if resp.status == 404:
                last_error = ChatGPTApiError("not found", status=404, url=url)
                continue
            data = await _read_json(resp)
            if isinstance(data, dict) and data.get("mapping"):
                return data
            last_error = ChatGPTApiError("conversation payload missing mapping", url=url)
        raise last_error or ChatGPTApiError(f"conversation not found: {conversation_id}")

    async def iter_conversation_summaries(self, *, page_size: int = DEFAULT_PAGE_SIZE):
        offset = 0
        while True:
            payload = await self.list_conversations(offset=offset, limit=page_size)
            total = int(payload.get("total") or 0)
            if total:
                self.last_list_total = total
            items = payload.get("items") or payload.get("conversations") or []
            if not isinstance(items, list):
                break
            for item in items:
                if isinstance(item, dict):
                    yield item
            total = int(payload.get("total") or 0)
            offset += len(items)
            if not items or (total and offset >= total):
                break
            await asyncio.sleep(REQUEST_DELAY_SEC)

    async def fetch_full_conversation(self, summary: dict[str, Any]) -> dict[str, Any]:
        conv_id = str(summary.get("id") or summary.get("conversation_id") or "")
        if not conv_id:
            raise ChatGPTApiError("conversation summary missing id")
        detail = await self.get_conversation(conv_id)
        detail.setdefault("conversation_id", conv_id)
        if "title" not in detail and summary.get("title"):
            detail["title"] = summary["title"]
        if "create_time" not in detail and summary.get("create_time"):
            detail["create_time"] = summary["create_time"]
        if "update_time" not in detail and summary.get("update_time"):
            detail["update_time"] = summary["update_time"]
        await asyncio.sleep(REQUEST_DELAY_SEC)
        return detail
