"""CLI for ChatGPT web collector."""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .config import collection_headless, hub_export_dir, staging_db_path, storage_state_path
from .export import export_changed
from .staging import StagingStore
from .sync import sync_to_hub


def _store() -> StagingStore:
    return StagingStore(staging_db_path())


async def _with_client(coro, *, headless: bool | None = None, use_profile: bool = False):
    from .browser import ChatGPTBrowser
    from .client import ChatGPTClient

    async with ChatGPTBrowser(headless=headless, use_profile=use_profile) as browser:
        client = ChatGPTClient(browser)
        return await coro(client, browser)


def _add_browser_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--headed",
        action="store_true",
        help="Visible browser with persistent profile (default)",
    )
    group.add_argument(
        "--headless",
        action="store_true",
        help="Headless mode using saved storage_state (try after headed works)",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Reuse login Chrome profile (can crash if profile is locked; default is storage_state)",
    )


def _print_collect_stats(label: str, stats) -> int:
    stale = getattr(stats, "stale_skipped", 0)
    print(
        f"{label}: listed={stats.listed} fetched={stats.fetched} "
        f"stored={stats.stored} skipped={stats.skipped} stale_skipped={stale} errors={stats.errors}"
    )
    if stats.error_reasons:
        print("error breakdown:")
        for reason, count in stats.error_reasons.most_common(5):
            print(f"  {count}x {reason}")
    if stats.errors and stats.stored:
        print("partial success — re-run backfill to retry rate-limited conversations")
        return 0
    return 0 if stats.errors == 0 else 2


def _resolve_headless(args: argparse.Namespace) -> bool:
    if getattr(args, "headless", False):
        return True
    if getattr(args, "headed", False):
        return False
    return collection_headless()


def cmd_login(args: argparse.Namespace) -> int:
    from .browser import AuthError, interactive_login

    try:
        path = asyncio.run(
            interactive_login(
                headless=False,
                timeout_minutes=args.timeout_minutes,
                use_chrome=not args.chromium,
            )
        )
        print(f"Saved session to {path}")
        return 0
    except AuthError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1


def cmd_backfill(args: argparse.Namespace) -> int:
    from .browser import AuthError, refresh_storage_state
    from .collector import backfill

    store = _store()
    headless = _resolve_headless(args)
    profile = getattr(args, "profile", False)
    if headless:
        mode = "headless (storage_state)"
    elif profile:
        mode = "headed (persistent profile)"
    else:
        mode = "headed (storage_state)"
    print(f"collect mode: {mode}")

    async def run(client, browser):
        stats = await backfill(store, client, max_conversations=args.max)
        if stats.fetched > 0:
            path = await refresh_storage_state(browser.context)
            print(f"refreshed storage_state: {path}")
        return stats

    try:
        stats = asyncio.run(_with_client(run, headless=headless, use_profile=profile))
        return _print_collect_stats("backfill", stats)
    except AuthError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1


def cmd_watch(args: argparse.Namespace) -> int:
    from .browser import AuthError, refresh_storage_state
    from .collector import watch

    store = _store()
    headless = _resolve_headless(args)
    profile = getattr(args, "profile", False)
    if headless:
        mode = "headless (storage_state)"
    elif profile:
        mode = "headed (persistent profile)"
    else:
        mode = "headed (storage_state)"
    print(f"collect mode: {mode}")

    async def run(client, browser):
        stats = await watch(store, client, lookback=args.lookback)
        if stats.fetched > 0:
            path = await refresh_storage_state(browser.context)
            print(f"refreshed storage_state: {path}")
        return stats

    try:
        stats = asyncio.run(_with_client(run, headless=headless, use_profile=profile))
        return _print_collect_stats("watch", stats)
    except AuthError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1


def cmd_export(_: argparse.Namespace) -> int:
    stats = export_changed(_store())
    print(f"export: exported={stats.exported} pending={stats.pending} total={stats.total}")
    print(f"hub dir: {hub_export_dir()}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    stats = sync_to_hub(_store(), api_url=args.api)
    print(
        f"sync: exported={stats.export.exported} "
        f"imported={stats.import_conversations} messages={stats.import_messages}"
    )
    if stats.import_errors:
        for err in stats.import_errors:
            print(f"  error: {err}", file=sys.stderr)
        return 2
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    store = _store()
    state = storage_state_path()
    print(f"version: {__version__}")
    print(f"collect default: {'headless' if collection_headless() else 'headed'}")
    print(f"auth state: {state} ({'ok' if state.is_file() else 'missing'})")
    print(f"staging db: {staging_db_path()} ({store.count()} conversations, {store.skipped_count()} skipped-stale)")
    pending = len(store.list_conversations(changed_only=True))
    print(f"pending export: {pending}")
    print(f"hub export dir: {hub_export_dir()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatgpt_collector")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Open browser for one-time ChatGPT authentication")
    p_login.add_argument(
        "--timeout-minutes",
        type=int,
        default=None,
        help="Max minutes to wait for you to finish login (default: 30)",
    )
    p_login.add_argument(
        "--chromium",
        action="store_true",
        help="Use bundled Chromium instead of installed Google Chrome",
    )

    p_backfill = sub.add_parser("backfill", help="Fetch all conversations from ChatGPT web")
    p_backfill.add_argument("--max", type=int, default=None, help="Stop after N conversations")
    _add_browser_flags(p_backfill)

    p_watch = sub.add_parser("watch", help="Incremental fetch of recent conversations")
    p_watch.add_argument("--lookback", type=int, default=50, help="Max recent threads to scan")
    _add_browser_flags(p_watch)

    sub.add_parser("export", help="Write changed staging rows to hub JSON files")
    p_sync = sub.add_parser("sync", help="Export then POST import to telemetry hub")
    p_sync.add_argument("--api", default=None, help="Hub API base URL")

    sub.add_parser("status", help="Show collector paths and counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "login": cmd_login,
        "backfill": cmd_backfill,
        "watch": cmd_watch,
        "export": cmd_export,
        "sync": cmd_sync,
        "status": cmd_status,
    }
    return handlers[args.command](args)
