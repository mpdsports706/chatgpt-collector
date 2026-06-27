"""Playwright session management for ChatGPT web."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .config import (
    LOGIN_TIMEOUT_MINUTES,
    browser_profile_dir,
    chatgpt_base_urls,
    collection_headless,
    collection_use_profile,
    storage_state_path,
)

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


@dataclass
class ChatGPTAuthSession:
    context: BrowserContext
    playwright: Playwright

    async def close(self) -> None:
        await self.context.close()
        await self.playwright.stop()


def chrome_channel_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            browser.close()
        return True
    except Exception:
        return False


async def open_persistent_session(
    *,
    headless: bool,
    use_chrome: bool = True,
) -> ChatGPTAuthSession:
    """Persistent Chrome profile — same session surface as interactive login (alt.xyz pattern)."""
    profile = browser_profile_dir()
    playwright = await async_playwright().start()
    launch_kwargs: dict = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if use_chrome:
        launch_kwargs["channel"] = "chrome"
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile),
            **launch_kwargs,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1440, "height": 900},
        )
    except Exception:
        if use_chrome:
            launch_kwargs.pop("channel", None)
            logger.warning("Google Chrome not found; falling back to Playwright Chromium")
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                **launch_kwargs,
                locale="en-US",
                timezone_id="America/Los_Angeles",
                viewport={"width": 1440, "height": 900},
            )
        else:
            await playwright.stop()
            raise
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return ChatGPTAuthSession(context=context, playwright=playwright)


async def open_auth_session(*, use_chrome: bool = True) -> ChatGPTAuthSession:
    return await open_persistent_session(headless=False, use_chrome=use_chrome)


async def first_page(session: ChatGPTAuthSession) -> Page:
    if session.context.pages:
        return session.context.pages[0]
    return await session.context.new_page()


async def verify_authenticated(context: BrowserContext) -> bool:
    """Best-effort API probe after manual login."""
    for base in chatgpt_base_urls():
        probe = f"{base}/backend-api/conversations?offset=0&limit=1&order=updated"
        try:
            resp = await context.request.get(probe)
            if resp.status == 200:
                return True
        except Exception:
            continue
    return False


async def refresh_storage_state(context: BrowserContext) -> Path:
    dest = storage_state_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(dest))
    return dest


def _print_login_instructions(
    *,
    target: Path,
    profile: Path,
    engine: str,
    timeout_minutes: int,
) -> None:
    print()
    print("ChatGPT login session")
    print("=====================")
    print(f"Browser engine: {engine}")
    print()
    print("IMPORTANT:")
    print("  - The browser stays open until YOU press Enter in this terminal.")
    print("  - Sign in with Google (or your provider) and complete any OTP/2FA steps.")
    print("  - Google may open a popup — finish the full flow before continuing.")
    print("  - Confirm you can see the ChatGPT chat UI (sidebar + message box).")
    print("  - Only then press Enter here to save the session.")
    print()
    print(f"Session file: {target}")
    print(f"Browser profile: {profile}")
    print(f"Waiting up to {timeout_minutes} minutes.")
    print()


async def interactive_login(
    *,
    headless: bool = False,
    timeout_minutes: int | None = None,
    use_chrome: bool = True,
) -> Path:
    """Open ChatGPT in a headed browser; user confirms when login is complete."""
    if headless:
        raise AuthError("Interactive login requires a visible browser (headless=False).")

    dest = storage_state_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = browser_profile_dir()
    wait_minutes = timeout_minutes if timeout_minutes is not None else LOGIN_TIMEOUT_MINUTES

    use_chrome_channel = use_chrome and chrome_channel_available()
    if use_chrome and not use_chrome_channel:
        logger.warning("Google Chrome not found; using Playwright Chromium for login")

    session = await open_auth_session(use_chrome=use_chrome_channel)
    page = await first_page(session)
    page.set_default_timeout(90_000)

    last_error: Exception | None = None
    for base in chatgpt_base_urls():
        try:
            await page.goto(base, wait_until="load", timeout=120_000)
            break
        except Exception as exc:
            last_error = exc
    else:
        await session.close()
        raise AuthError(f"Could not reach ChatGPT: {last_error}") from last_error

    engine = "chrome" if use_chrome_channel else "chromium"
    _print_login_instructions(
        target=dest,
        profile=profile,
        engine=engine,
        timeout_minutes=wait_minutes,
    )

    loop = asyncio.get_running_loop()
    prompt = "Press Enter ONLY after ChatGPT is fully loaded and you can start a chat... "
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, input, prompt),
            timeout=wait_minutes * 60,
        )
    except asyncio.TimeoutError as exc:
        await session.close()
        raise AuthError(
            f"Login timed out after {wait_minutes} minutes without confirmation."
        ) from exc

    if not await verify_authenticated(session.context):
        print(
            "Warning: backend-api probe did not return 200 yet. "
            "Saving session anyway — re-run make chatgpt-login if imports fail.",
            flush=True,
        )

    dest = await refresh_storage_state(session.context)
    await session.close()
    logger.info("Saved ChatGPT session to %s", dest)
    return dest


class ChatGPTBrowser:
    """Authenticated browser for collection.

    Default headed mode: visible Chrome + saved storage_state (stable on macOS).
    Set CHATGPT_COLLECTOR_USE_PROFILE=true to reuse the login persistent profile.
    """

    def __init__(
        self,
        *,
        headless: bool | None = None,
        use_chrome: bool = True,
        use_profile: bool | None = None,
    ) -> None:
        self.headless = collection_headless() if headless is None else headless
        self.use_chrome = use_chrome
        self.use_profile = collection_use_profile() if use_profile is None else use_profile
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._session: ChatGPTAuthSession | None = None
        self._page: Page | None = None

    async def _open_storage_context(self, *, headless: bool, use_chrome_channel: bool) -> None:
        state = storage_state_path()
        if not state.is_file():
            raise AuthError(
                f"Missing auth state at {state}. Run from repo root: make chatgpt-login"
            )
        self._playwright = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if use_chrome_channel:
            launch_kwargs["channel"] = "chrome"
        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception:
            launch_kwargs.pop("channel", None)
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(storage_state=str(state))
        self._page = await self._context.new_page()
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    async def __aenter__(self) -> "ChatGPTBrowser":
        use_chrome_channel = self.use_chrome and chrome_channel_available()

        if not self.headless and self.use_profile:
            self._session = await open_persistent_session(
                headless=False,
                use_chrome=use_chrome_channel,
            )
            self._context = self._session.context
            self._page = await first_page(self._session)
        else:
            await self._open_storage_context(
                headless=self.headless,
                use_chrome_channel=use_chrome_channel,
            )

        self._page.set_default_timeout(90_000)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._session:
            await self._session.close()
        elif self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser session not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser session not started")
        return self._context
