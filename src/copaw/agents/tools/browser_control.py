# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""Browser automation tool using Playwright.

Single tool with action-based API matching browser MCP: start, stop, open,
navigate, navigate_back, screenshot, snapshot, click, type, eval, evaluate,
resize, console_messages, handle_dialog, file_upload, fill_form, install,
press_key, network_requests, run_code, drag, hover, select_option, tabs,
wait_for, pdf, close, get_cookies, set_cookies, save_storage_state,
load_storage_state. Uses refs from snapshot for ref-based actions.
"""

import asyncio
import atexit
from concurrent import futures
from dataclasses import dataclass, field
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable, Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config import (
    get_playwright_chromium_executable_path,
    get_system_default_browser,
    is_running_in_container,
)

from .browser_snapshot import build_role_snapshot_from_aria

logger = logging.getLogger(__name__)

# Hybrid mode detection: Windows + Uvicorn reload mode requires sync Playwright
# to avoid NotImplementedError with asyncio.create_subprocess_exec.
# On other platforms or without reload, use async Playwright for better performance.
_USE_SYNC_PLAYWRIGHT = (
    sys.platform == "win32" and os.environ.get("COPAW_RELOAD_MODE") == "1"
)

if _USE_SYNC_PLAYWRIGHT:
    _executor: Optional[futures.ThreadPoolExecutor] = None

    def _get_executor() -> futures.ThreadPoolExecutor:
        global _executor
        if _executor is None:
            _executor = futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="playwright",
            )
        return _executor

    async def _run_sync(func, *args, **kwargs):
        """Run a sync function in the thread pool and await the result."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _get_executor(),
            lambda: func(*args, **kwargs),
        )

else:

    async def _run_sync(func, *args, **kwargs):
        """Fallback: directly call async function (should not be used in async mode)."""
        return await func(*args, **kwargs)


# ---------------------------------------------------------------------------
# Per-agent browser context (isolated cookies/session per agent)
# ---------------------------------------------------------------------------


@dataclass
class AgentBrowserContext:
    """Per-agent browser state: owns a Playwright BrowserContext."""

    agent_id: str
    context: Any = None
    pages: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, dict] = field(default_factory=dict)
    refs_frame: dict[str, Any] = field(default_factory=dict)
    console_logs: dict[str, list] = field(default_factory=dict)
    network_requests: dict[str, list] = field(default_factory=dict)
    pending_dialogs: dict[str, list] = field(default_factory=dict)
    pending_file_choosers: dict[str, list] = field(default_factory=dict)
    current_page_id: str | None = None
    page_counter: int = 0
    last_activity_time: float = 0.0
    _sync_context: Any = None
    _creating_page: bool = False
    _pending_session_storage: list = field(default_factory=list)


# Shared browser process state (single Playwright instance + browser)
_browser_state: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "headless": True,
    "browser_kind": None,  # "chromium" or "webkit"
    "_idle_task": None,
    "_last_browser_error": None,
    "_sync_browser": None,
    "_sync_playwright": None,
}

# Per-agent contexts keyed by agent_id
_agent_contexts: dict[str, AgentBrowserContext] = {}

# Auto-persist storage state directory
_STORAGE_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".copaw", "browser_state"
)


def _storage_state_path(agent_id: str) -> str:
    """Return the auto-persist file path for an agent's storage state."""
    safe_id = agent_id.replace("/", "_").replace("\\", "_")
    return os.path.join(_STORAGE_STATE_DIR, f"{safe_id}.json")


def _get_agent_id() -> str:
    """Get the current agent ID from context."""
    from ...app.agent_context import get_current_agent_id

    return get_current_agent_id()


def _get_agent_ctx(
    agent_id: str = "",
) -> AgentBrowserContext | None:
    """Get the AgentBrowserContext for the given (or current) agent."""
    if not agent_id:
        agent_id = _get_agent_id()
    return _agent_contexts.get(agent_id)


def _get_or_create_agent_ctx(
    agent_id: str = "",
) -> AgentBrowserContext:
    """Get or create the AgentBrowserContext for the given agent."""
    if not agent_id:
        agent_id = _get_agent_id()
    if agent_id not in _agent_contexts:
        _agent_contexts[agent_id] = AgentBrowserContext(
            agent_id=agent_id,
        )
    return _agent_contexts[agent_id]


# Stop the browser after this many seconds of inactivity (default 30 minutes).
_BROWSER_IDLE_TIMEOUT = 1800.0

# ---------------------------------------------------------------------------
# Lifecycle callback system (for browser_live_view router)
# ---------------------------------------------------------------------------
_lifecycle_callbacks: list[Callable] = []


def register_browser_lifecycle_callback(cb: Callable) -> None:
    """Register a callback to be notified of browser lifecycle events."""
    if cb not in _lifecycle_callbacks:
        _lifecycle_callbacks.append(cb)


def unregister_browser_lifecycle_callback(cb: Callable) -> None:
    """Unregister a previously registered lifecycle callback."""
    try:
        _lifecycle_callbacks.remove(cb)
    except ValueError:
        pass


async def _notify_lifecycle(event: str, **kwargs: Any) -> None:
    """Notify all registered callbacks of a lifecycle event."""
    for cb in list(_lifecycle_callbacks):
        try:
            result = cb(event, **kwargs)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug(
                "Lifecycle callback error for event=%s",
                event,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Public access functions (for router / live-view)
# ---------------------------------------------------------------------------


def get_browser_state_summary(agent_id: str = "") -> dict:
    """Return a summary of the current browser state for an agent."""
    running = _is_browser_running()
    ctx = _get_agent_ctx(agent_id)
    page_id = ctx.current_page_id if ctx else None
    url = ""
    viewport = {"width": 1280, "height": 720}
    if running and ctx and page_id:
        page = ctx.pages.get(page_id)
        if page:
            try:
                url = page.url
            except Exception:
                pass
            try:
                vp = page.viewport_size
                if vp:
                    viewport = {
                        "width": vp.get("width", 1280),
                        "height": vp.get("height", 720),
                    }
            except Exception:
                pass
    return {
        "running": running,
        "headless": _browser_state.get("headless", True),
        "current_page_id": page_id,
        "url": url,
        "viewport": viewport,
        "agent_id": ctx.agent_id if ctx else (agent_id or "default"),
    }


def is_browser_running() -> bool:
    """Public: check if the shared browser process is running."""
    return _is_browser_running()


async def get_browser_tabs(agent_id: str = "") -> list[dict]:
    """Return a list of open tabs for an agent.

    Each entry: ``{"page_id": str, "url": str, "title": str,
    "active": bool}``.
    """
    ctx = _get_agent_ctx(agent_id)
    if not ctx or not _is_browser_running():
        return []
    tabs: list[dict] = []
    for pid, page in ctx.pages.items():
        url = ""
        title = ""
        try:
            url = page.url
        except Exception:
            pass
        try:
            if _USE_SYNC_PLAYWRIGHT:
                title = page.title()
            else:
                title = await page.title()
        except Exception:
            pass
        tabs.append(
            {
                "page_id": pid,
                "url": url,
                "title": title or url or pid,
                "active": pid == ctx.current_page_id,
            },
        )
    return tabs


def set_current_page(page_id: str, agent_id: str = "") -> bool:
    """Switch the active page for an agent.  Returns True on success."""
    ctx = _get_agent_ctx(agent_id)
    if not ctx or page_id not in ctx.pages:
        return False
    ctx.current_page_id = page_id
    return True


def get_browser_kind() -> str | None:
    """Return the browser engine kind: ``"chromium"``, ``"webkit"``,
    or ``None`` if no browser is running."""
    return _browser_state.get("browser_kind")


def is_agent_browser_active(agent_id: str = "") -> bool:
    """Check if the given agent has an active browser context."""
    ctx = _get_agent_ctx(agent_id)
    return ctx is not None and ctx.context is not None


def get_page(page_id: str = "", agent_id: str = ""):
    """Public: get page for an agent. Falls back to current page."""
    ctx = _get_agent_ctx(agent_id)
    if not ctx:
        return None
    if not page_id:
        page_id = ctx.current_page_id or ""
    return ctx.pages.get(page_id)


async def create_new_tab(agent_id: str = "") -> dict:
    """Public: create a new blank tab for an agent.

    Returns ``{"ok": True, "page_id": ...}`` on success.
    """
    if not agent_id:
        agent_id = _get_agent_id()
    ctx = _get_agent_ctx(agent_id)
    if not ctx or not _is_browser_running():
        return {"ok": False, "error": "Browser not running"}
    try:
        ctx._creating_page = True
        try:
            if _USE_SYNC_PLAYWRIGHT:
                page = await _run_sync(ctx._sync_context.new_page)
            else:
                page = await ctx.context.new_page()
        finally:
            ctx._creating_page = False
        new_id = _next_page_id(ctx)
        ctx.refs[new_id] = {}
        ctx.console_logs[new_id] = []
        ctx.network_requests[new_id] = []
        ctx.pending_dialogs[new_id] = []
        ctx.pending_file_choosers[new_id] = []
        _attach_page_listeners(page, new_id, ctx)
        ctx.pages[new_id] = page
        ctx.current_page_id = new_id
        await _notify_lifecycle(
            "navigated",
            url="about:blank",
            page_id=new_id,
            agent_id=ctx.agent_id,
        )
        return {"ok": True, "page_id": new_id}
    except Exception as e:
        return {"ok": False, "error": f"New tab failed: {e!s}"}


async def close_tab_by_id(
    page_id: str,
    agent_id: str = "",
) -> dict:
    """Public: close a specific tab by page_id.

    Returns ``{"ok": True}`` on success.
    """
    if not agent_id:
        agent_id = _get_agent_id()
    ctx = _get_agent_ctx(agent_id)
    if not ctx:
        return {"ok": False, "error": "No browser context"}
    page = ctx.pages.get(page_id)
    if not page:
        return {"ok": False, "error": f"Page '{page_id}' not found"}
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(page.close)
        else:
            await page.close()
        ctx.pages.pop(page_id, None)
        ctx.refs.pop(page_id, None)
        ctx.refs_frame.pop(page_id, None)
        ctx.console_logs.pop(page_id, None)
        ctx.network_requests.pop(page_id, None)
        ctx.pending_dialogs.pop(page_id, None)
        ctx.pending_file_choosers.pop(page_id, None)
        if ctx.current_page_id == page_id:
            remaining = list(ctx.pages.keys())
            ctx.current_page_id = remaining[0] if remaining else None
        return {"ok": True, "page_id": ctx.current_page_id}
    except Exception as e:
        return {"ok": False, "error": f"Close tab failed: {e!s}"}


def touch_activity(agent_id: str = "") -> None:
    """Public: reset idle timer for an agent."""
    _touch_activity(agent_id)


def _touch_activity(agent_id: str = "") -> None:
    """Record the current time as the last activity for an agent."""
    ctx = _get_agent_ctx(agent_id)
    if ctx:
        ctx.last_activity_time = time.monotonic()


def _is_browser_running() -> bool:
    """Check if the shared browser process is running."""
    if _USE_SYNC_PLAYWRIGHT:
        return _browser_state.get("_sync_browser") is not None
    return _browser_state.get("browser") is not None


def _close_agent_context(agent_id: str) -> None:
    """Close a single agent's browser context and clean up."""
    ctx = _agent_contexts.pop(agent_id, None)
    if ctx is None:
        return
    # Auto-save storage state (cookies + localStorage + sessionStorage).
    # Note: on macOS (async Playwright), storage_state() / evaluate()
    # return coroutines. In this sync atexit path we cannot await them,
    # so we only attempt save when using sync Playwright (Windows).
    # The normal close path (_async_close_agent_context) handles all
    # platforms correctly.
    if _USE_SYNC_PLAYWRIGHT and ctx._sync_context is not None:
        try:
            state_path = _storage_state_path(agent_id)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            state = ctx._sync_context.storage_state()
            session_storage: list[dict] = []
            for pid, page in ctx.pages.items():
                try:
                    ss = page.evaluate(
                        "(() => {"
                        "  const d = {};"
                        "  for (let i = 0;"
                        " i < sessionStorage.length; i++) {"
                        "    const k = sessionStorage.key(i);"
                        "    d[k] = sessionStorage.getItem(k);"
                        "  }"
                        "  return {"
                        " origin: location.origin, data: d };"
                        "})()"
                    )
                    if ss and ss.get("data"):
                        session_storage.append(ss)
                except Exception:
                    pass
            state["sessionStorage"] = session_storage
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    # Close all pages first, then the context
    for page in list(ctx.pages.values()):
        try:
            page.close()
        except Exception:
            pass
    ctx.pages.clear()
    if _USE_SYNC_PLAYWRIGHT:
        if ctx._sync_context is not None:
            try:
                ctx._sync_context.close()
            except Exception:
                pass
            ctx._sync_context = None
    else:
        if ctx.context is not None:
            try:
                # sync close since we may be in atexit
                ctx.context.close()
            except Exception:
                pass
            ctx.context = None


async def _async_close_agent_context(agent_id: str) -> None:
    """Async version: close a single agent's browser context."""
    ctx = _agent_contexts.pop(agent_id, None)
    if ctx is None:
        return
    # Auto-save storage state (cookies + localStorage + sessionStorage)
    context = ctx._sync_context if _USE_SYNC_PLAYWRIGHT else ctx.context
    if context is not None:
        try:
            state_path = _storage_state_path(agent_id)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            if _USE_SYNC_PLAYWRIGHT:
                state = await _run_sync(context.storage_state)
            else:
                state = await context.storage_state()
            # Also capture sessionStorage from all pages
            session_storage: list[dict] = []
            for pid, page in ctx.pages.items():
                try:
                    js = (
                        "(() => {"
                        "  const d = {};"
                        "  for (let i = 0; i < sessionStorage.length; i++) {"
                        "    const k = sessionStorage.key(i);"
                        "    d[k] = sessionStorage.getItem(k);"
                        "  }"
                        "  return { origin: location.origin, data: d };"
                        "})()"
                    )
                    if _USE_SYNC_PLAYWRIGHT:
                        ss = await _run_sync(page.evaluate, js)
                    else:
                        ss = await page.evaluate(js)
                    if ss and ss.get("data"):
                        session_storage.append(ss)
                except Exception:
                    pass
            state["sessionStorage"] = session_storage
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info(
                "Auto-saved storage state for agent '%s' to %s"
                " (cookies=%d, sessionStorage=%d origins)",
                agent_id,
                state_path,
                len(state.get("cookies", [])),
                len(session_storage),
            )
        except Exception as e:
            logger.warning(
                "Failed to auto-save storage state: %s", e,
            )
    for page in list(ctx.pages.values()):
        try:
            if _USE_SYNC_PLAYWRIGHT:
                page.close()
            else:
                await page.close()
        except Exception:
            pass
    ctx.pages.clear()
    if _USE_SYNC_PLAYWRIGHT:
        if ctx._sync_context is not None:
            try:
                ctx._sync_context.close()
            except Exception:
                pass
            ctx._sync_context = None
    else:
        if ctx.context is not None:
            try:
                await ctx.context.close()
            except Exception:
                pass
            ctx.context = None


def _stop_browser_process() -> None:
    """Close the shared browser process and playwright (sync)."""
    if _USE_SYNC_PLAYWRIGHT:
        sb = _browser_state.get("_sync_browser")
        if sb is not None:
            try:
                sb.close()
            except Exception:
                pass
        sp = _browser_state.get("_sync_playwright")
        if sp is not None:
            try:
                sp.stop()
            except Exception:
                pass
        _browser_state["_sync_browser"] = None
        _browser_state["_sync_playwright"] = None
    else:
        b = _browser_state.get("browser")
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
        pw = _browser_state.get("playwright")
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        _browser_state["browser"] = None
        _browser_state["playwright"] = None
    _browser_state["headless"] = True
    _browser_state["browser_kind"] = None
    _browser_state["_last_browser_error"] = None


async def _async_stop_browser_process() -> None:
    """Async version: close the shared browser process."""
    if _USE_SYNC_PLAYWRIGHT:
        sb = _browser_state.get("_sync_browser")
        if sb is not None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    _get_executor(),
                    sb.close,
                )
            except Exception:
                pass
        sp = _browser_state.get("_sync_playwright")
        if sp is not None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    _get_executor(),
                    sp.stop,
                )
            except Exception:
                pass
        _browser_state["_sync_browser"] = None
        _browser_state["_sync_playwright"] = None
    else:
        b = _browser_state.get("browser")
        if b is not None:
            try:
                await b.close()
            except Exception:
                pass
        pw = _browser_state.get("playwright")
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        _browser_state["browser"] = None
        _browser_state["playwright"] = None
    _browser_state["headless"] = True
    _browser_state["browser_kind"] = None
    _browser_state["_last_browser_error"] = None


async def _idle_watchdog(
    idle_seconds: float = _BROWSER_IDLE_TIMEOUT,
) -> None:
    """Background task: check all agent contexts for idle timeout.

    Closes individual agent contexts that have been idle, and shuts down
    the shared browser process when no agents remain.
    """
    try:
        while True:
            await asyncio.sleep(60)  # check every minute
            if not _is_browser_running():
                return
            now = time.monotonic()
            expired = [
                aid
                for aid, ctx in list(_agent_contexts.items())
                if (now - ctx.last_activity_time) >= idle_seconds
            ]
            for aid in expired:
                logger.info(
                    "Agent '%s' browser context idle, closing",
                    aid,
                )
                await _async_close_agent_context(aid)
                await _notify_lifecycle(
                    "stopped",
                    agent_id=aid,
                )
            if not _agent_contexts:
                logger.info(
                    "No active agent contexts, stopping browser",
                )
                await _async_stop_browser_process()
                return
    except asyncio.CancelledError:
        pass


def _atexit_cleanup() -> None:
    """Best-effort browser cleanup registered with :func:`atexit`."""
    if not _is_browser_running():
        return
    # Close all agent contexts synchronously
    for aid in list(_agent_contexts.keys()):
        _close_agent_context(aid)
    _stop_browser_process()


atexit.register(_atexit_cleanup)


def _tool_response(text: str) -> ToolResponse:
    """Wrap text for agentscope Toolkit (return ToolResponse)."""
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
    )


def _chromium_launch_args() -> list[str]:
    """Extra args for Chromium when running in container."""
    if is_running_in_container():
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []


def _chromium_executable_path() -> str | None:
    """Chromium executable path when set (e.g. container); else None."""
    return get_playwright_chromium_executable_path()


def _use_webkit_fallback() -> bool:
    """True only on macOS when no system Chrome/Edge/Chromium found.
    Use WebKit (Safari) to avoid downloading Chromium. Windows has no system
    WebKit, so we never use webkit there.
    """
    return sys.platform == "darwin" and _chromium_executable_path() is None


def _ensure_playwright_async():
    """Import async_playwright; raise ImportError with hint if missing."""
    try:
        from playwright.async_api import async_playwright

        return async_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright not installed. Use the same Python that runs CoPaw (e.g. "
            "activate your venv or use 'uv run'): "
            f"'{sys.executable}' -m pip install playwright && "
            f"'{sys.executable}' -m playwright install",
        ) from exc


def _ensure_playwright_sync():
    """Import sync_playwright; raise ImportError with hint if missing."""
    try:
        from playwright.sync_api import sync_playwright

        return sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright not installed. Use the same Python that runs CoPaw (e.g. "
            "activate your venv or use 'uv run'): "
            f"'{sys.executable}' -m pip install playwright && "
            f"'{sys.executable}' -m playwright install",
        ) from exc


def _sync_browser_launch(headless: bool):
    """Launch browser using sync Playwright (for hybrid mode).
    Returns (playwright, browser, kind) — context creation is per-agent.
    *kind* is ``"chromium"`` or ``"webkit"``."""
    sync_playwright = _ensure_playwright_sync()
    pw = sync_playwright().start()  # Start without context manager
    use_default = not is_running_in_container() and os.environ.get(
        "COPAW_BROWSER_USE_DEFAULT",
        "1",
    ).strip().lower() in ("1", "true", "yes")
    default_kind, default_path = (
        get_system_default_browser() if use_default else (None, None)
    )
    exe: Optional[str] = None
    if default_kind == "chromium" and default_path:
        exe = default_path
    elif default_kind != "webkit":
        exe = _chromium_executable_path()

    kind: str = "chromium"
    if exe:
        launch_kwargs = {"headless": headless}
        extra_args = _chromium_launch_args()
        if extra_args:
            launch_kwargs["args"] = extra_args
        launch_kwargs["executable_path"] = exe
        browser = pw.chromium.launch(**launch_kwargs)
    elif default_kind == "webkit" or sys.platform == "darwin":
        browser = pw.webkit.launch(headless=headless)
        kind = "webkit"
    else:
        launch_kwargs = {"headless": headless}
        extra_args = _chromium_launch_args()
        if extra_args:
            launch_kwargs["args"] = extra_args
        browser = pw.chromium.launch(**launch_kwargs)

    return pw, browser, kind


def _parse_json_param(value: str, default: Any = None):
    """Parse optional JSON string param (e.g. fields, paths, values)."""
    if not value or not isinstance(value, str):
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        if "," in value:
            return [x.strip() for x in value.split(",")]
        return default


async def browser_use(  # pylint: disable=R0911,R0912
    action: str,
    url: str = "",
    page_id: str = "default",
    selector: str = "",
    text: str = "",
    code: str = "",
    path: str = "",
    wait: int = 0,
    full_page: bool = False,
    width: int = 0,
    height: int = 0,
    level: str = "info",
    filename: str = "",
    accept: bool = True,
    prompt_text: str = "",
    ref: str = "",
    element: str = "",
    paths_json: str = "",
    fields_json: str = "",
    key: str = "",
    submit: bool = False,
    slowly: bool = False,
    include_static: bool = False,
    screenshot_type: str = "png",
    snapshot_filename: str = "",
    double_click: bool = False,
    button: str = "left",
    modifiers_json: str = "",
    start_ref: str = "",
    end_ref: str = "",
    start_selector: str = "",
    end_selector: str = "",
    start_element: str = "",
    end_element: str = "",
    values_json: str = "",
    tab_action: str = "",
    index: int = -1,
    wait_time: float = 0,
    text_gone: str = "",
    frame_selector: str = "",
    headed: bool = False,
    cookies_json: str = "",
    cookies_file: str = "",
    cookies_url: str = "",
) -> ToolResponse:
    """Control browser (Playwright). Default is headless. Use headed=True with
    action=start to open a visible browser window. Flow: start, open(url),
    snapshot to get refs, then click/type etc. with ref or selector. Use
    page_id for multiple tabs.

    Args:
        action (str):
            Required. Action type. Values: start, stop, open, navigate,
            navigate_back, snapshot, screenshot, click, type, eval, evaluate,
            resize, console_messages, network_requests, handle_dialog,
            file_upload, fill_form, install, press_key, run_code, drag, hover,
            select_option, tabs, wait_for, pdf, close, get_cookies,
            set_cookies, save_storage_state, load_storage_state.
        url (str):
            URL to open. Required for action=open or navigate.
        page_id (str):
            Page/tab identifier, default "default". Use different page_id for
            multiple tabs.
        selector (str):
            CSS selector to locate element for click/type/hover etc. Prefer
            ref when available.
        text (str):
            Text to type. Required for action=type.
        code (str):
            JavaScript code. Required for action=eval, evaluate, or run_code.
        path (str):
            File path for screenshot save or PDF export.
        wait (int):
            Milliseconds to wait after click. Used with action=click.
        full_page (bool):
            Whether to capture full page. Used with action=screenshot.
        width (int):
            Viewport width in pixels. Used with action=resize.
        height (int):
            Viewport height in pixels. Used with action=resize.
        level (str):
            Console log level filter, e.g. "info" or "error". Used with
            action=console_messages.
        filename (str):
            Filename for saving logs or screenshot. Used with
            console_messages, network_requests, screenshot.
        accept (bool):
            Whether to accept dialog (true) or dismiss (false). Used with
            action=handle_dialog.
        prompt_text (str):
            Input for prompt dialog. Used with action=handle_dialog when
            dialog is prompt.
        ref (str):
            Element ref from snapshot output; use for stable targeting. Prefer
            ref for click/type/hover/screenshot/evaluate/select_option.
        element (str):
            Element description for evaluate etc. Prefer ref when available.
        paths_json (str):
            JSON array string of file paths. Used with action=file_upload.
        fields_json (str):
            JSON object string of form field name to value. Used with
            action=fill_form.
        key (str):
            Key name, e.g. "Enter", "Control+a". Required for
            action=press_key.
        submit (bool):
            Whether to submit (press Enter) after typing. Used with
            action=type.
        slowly (bool):
            Whether to type character by character. Used with action=type.
        include_static (bool):
            Whether to include static resource requests. Used with
            action=network_requests.
        screenshot_type (str):
            Screenshot format, "png" or "jpeg". Used with action=screenshot.
        snapshot_filename (str):
            File path to save snapshot output. Used with action=snapshot.
        double_click (bool):
            Whether to double-click. Used with action=click.
        button (str):
            Mouse button: "left", "right", or "middle". Used with
            action=click.
        modifiers_json (str):
            JSON array of modifier keys, e.g. ["Shift","Control"]. Used with
            action=click.
        start_ref (str):
            Drag start element ref. Used with action=drag.
        end_ref (str):
            Drag end element ref. Used with action=drag.
        start_selector (str):
            Drag start CSS selector. Used with action=drag.
        end_selector (str):
            Drag end CSS selector. Used with action=drag.
        start_element (str):
            Drag start element description. Used with action=drag.
        end_element (str):
            Drag end element description. Used with action=drag.
        values_json (str):
            JSON of option value(s) for select. Used with
            action=select_option.
        tab_action (str):
            Tab action: list, new, close, or select. Required for
            action=tabs.
        index (int):
            Tab index for tabs select, zero-based. Used with action=tabs.
        wait_time (float):
            Seconds to wait. Used with action=wait_for.
        text_gone (str):
            Wait until this text disappears from page. Used with
            action=wait_for.
        frame_selector (str):
            iframe selector, e.g. "iframe#main". Set when operating inside
            that iframe in snapshot/click/type etc.
        headed (bool):
            When True with action=start, launch a visible browser window
            (non-headless). User can see the real browser. Default False.
        cookies_json (str):
            JSON array of cookie objects for action=set_cookies. Each cookie
            must have "name", "value", "url" or "domain"+"path". Example:
            [{"name":"token","value":"abc","url":"http://example.com"}]
        cookies_file (str):
            File path for action=get_cookies (save) or set_cookies (load).
            get_cookies saves cookies to this file. set_cookies loads from
            this file (ignored if cookies_json is provided).
        cookies_url (str):
            Filter cookies by URL for action=get_cookies. If empty, returns
            all cookies. Example: "http://192.168.3.123:31813"
    """
    action = (action or "").strip().lower()
    if not action:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "action required"},
                ensure_ascii=False,
                indent=2,
            ),
        )

    page_id = (page_id or "default").strip() or "default"
    ctx = _get_agent_ctx()
    current = ctx.current_page_id if ctx else None
    pages = ctx.pages if ctx else {}
    if page_id == "default" and current and current in pages:
        page_id = current

    try:
        if action == "start":
            return await _action_start(headed=headed)
        if action == "stop":
            return await _action_stop()
        if action == "open":
            return await _action_open(url, page_id)
        if action == "navigate":
            return await _action_navigate(url, page_id)
        if action == "navigate_back":
            return await _action_navigate_back(page_id)
        if action in ("screenshot", "take_screenshot"):
            return await _action_screenshot(
                page_id,
                path or filename,
                full_page,
                screenshot_type,
                ref,
                element,
                frame_selector,
            )
        if action == "snapshot":
            return await _action_snapshot(
                page_id,
                snapshot_filename or filename,
                frame_selector,
            )
        if action == "click":
            return await _action_click(
                page_id,
                selector,
                ref,
                element,
                wait,
                double_click,
                button,
                modifiers_json,
                frame_selector,
            )
        if action == "type":
            return await _action_type(
                page_id,
                selector,
                ref,
                element,
                text,
                submit,
                slowly,
                frame_selector,
            )
        if action == "eval":
            return await _action_eval(page_id, code)
        if action == "evaluate":
            return await _action_evaluate(
                page_id,
                code,
                ref,
                element,
                frame_selector,
            )
        if action == "resize":
            return await _action_resize(page_id, width, height)
        if action == "console_messages":
            return await _action_console_messages(
                page_id,
                level,
                filename or path,
            )
        if action == "handle_dialog":
            return await _action_handle_dialog(page_id, accept, prompt_text)
        if action == "file_upload":
            return await _action_file_upload(page_id, paths_json)
        if action == "fill_form":
            return await _action_fill_form(page_id, fields_json)
        if action == "install":
            return await _action_install()
        if action == "press_key":
            return await _action_press_key(page_id, key)
        if action == "network_requests":
            return await _action_network_requests(
                page_id,
                include_static,
                filename or path,
            )
        if action == "run_code":
            return await _action_run_code(page_id, code)
        if action == "drag":
            return await _action_drag(
                page_id,
                start_ref,
                end_ref,
                start_selector,
                end_selector,
                start_element,
                end_element,
                frame_selector,
            )
        if action == "hover":
            return await _action_hover(
                page_id,
                ref,
                element,
                selector,
                frame_selector,
            )
        if action == "select_option":
            return await _action_select_option(
                page_id,
                ref,
                element,
                values_json,
                frame_selector,
            )
        if action == "tabs":
            return await _action_tabs(page_id, tab_action, index)
        if action == "wait_for":
            return await _action_wait_for(page_id, wait_time, text, text_gone)
        if action == "pdf":
            return await _action_pdf(page_id, path)
        if action == "close":
            return await _action_close(page_id)
        if action == "get_cookies":
            return await _action_get_cookies(cookies_url, cookies_file)
        if action == "set_cookies":
            return await _action_set_cookies(
                cookies_json, cookies_file
            )
        if action == "save_storage_state":
            return await _action_save_storage_state(cookies_file)
        if action == "load_storage_state":
            return await _action_load_storage_state(cookies_file)
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Unknown action: {action}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        logger.error("Browser tool error: %s", e, exc_info=True)
        return _tool_response(
            json.dumps(
                {"ok": False, "error": str(e)},
                ensure_ascii=False,
                indent=2,
            ),
        )


def _get_page(page_id: str):
    """Return page for page_id or None if not found."""
    ctx = _get_agent_ctx()
    if not ctx:
        return None
    return ctx.pages.get(page_id)


def _get_refs(page_id: str) -> dict[str, dict]:
    """Return refs map for page_id (ref -> {role, name?, nth?})."""
    ctx = _get_agent_ctx()
    if not ctx:
        return {}
    return ctx.refs.setdefault(page_id, {})


def _get_root(page, _page_id: str, frame_selector: str = ""):
    """Return page or frame for frame_selector (ref/selector)."""
    if not (frame_selector and frame_selector.strip()):
        return page
    return page.frame_locator(frame_selector.strip())


def _get_locator_by_ref(
    page,
    page_id: str,
    ref: str,
    frame_selector: str = "",
):
    """Resolve snapshot ref to locator; frame_selector for iframe."""
    refs = _get_refs(page_id)
    info = refs.get(ref)
    if not info:
        return None
    role = info.get("role", "generic")
    name = info.get("name")
    nth = info.get("nth", 0)
    root = _get_root(page, page_id, frame_selector)
    locator = root.get_by_role(role, name=name or None)
    if nth is not None and nth > 0:
        locator = locator.nth(nth)
    return locator


def _attach_page_listeners(
    page,
    page_id: str,
    ctx: AgentBrowserContext,
) -> None:
    """Attach console and request listeners for a page.
    Captures *ctx* in closures so callbacks work even if ContextVar changes."""
    logs = ctx.console_logs.setdefault(page_id, [])

    def on_console(msg):
        logs.append({"level": msg.type, "text": msg.text})

    page.on("console", on_console)
    requests_list = ctx.network_requests.setdefault(page_id, [])

    def on_request(req):
        requests_list.append(
            {
                "url": req.url,
                "method": req.method,
                "resourceType": getattr(req, "resource_type", None),
            },
        )

    def on_response(res):
        for r in requests_list:
            if r.get("url") == res.url and "status" not in r:
                r["status"] = res.status
                break

    page.on("request", on_request)
    page.on("response", on_response)
    dialogs = ctx.pending_dialogs.setdefault(page_id, [])

    def on_dialog(dialog):
        dialogs.append(dialog)

    page.on("dialog", on_dialog)
    choosers = ctx.pending_file_choosers.setdefault(page_id, [])

    def on_filechooser(chooser):
        choosers.append(chooser)

    page.on("filechooser", on_filechooser)


def _next_page_id(ctx: AgentBrowserContext) -> str:
    """Return a unique page_id (page_N) for the given agent context."""
    ctx.page_counter += 1
    return f"page_{ctx.page_counter}"


def _attach_context_listeners(
    context,
    ctx: AgentBrowserContext,
) -> None:
    """When the page opens a new tab (e.g. target=_blank, window.open),
    register it under the agent's context.
    Captures *ctx* so callbacks work even if ContextVar changes."""

    def on_page(page):
        # Skip if page is being created programmatically (create_new_tab,
        # _action_open, etc.) — the caller will register it.
        if ctx._creating_page:
            return
        new_id = _next_page_id(ctx)
        ctx.refs[new_id] = {}
        ctx.console_logs[new_id] = []
        ctx.network_requests[new_id] = []
        ctx.pending_dialogs[new_id] = []
        ctx.pending_file_choosers[new_id] = []
        _attach_page_listeners(page, new_id, ctx)
        ctx.pages[new_id] = page
        ctx.current_page_id = new_id
        logger.debug(
            "New tab opened by page, registered as page_id=%s",
            new_id,
        )

    context.on("page", on_page)


async def _ensure_browser(agent_id: str = "") -> bool:  # pylint: disable=too-many-branches
    """Two-phase ensure: (1) shared browser process, (2) agent context.
    Return True if ready, False on failure."""
    ctx = _get_or_create_agent_ctx(agent_id)

    # Phase 1: ensure shared browser process
    if not _is_browser_running():
        try:
            if _USE_SYNC_PLAYWRIGHT:
                loop = asyncio.get_event_loop()
                pw, browser, kind = await loop.run_in_executor(
                    _get_executor(),
                    lambda: _sync_browser_launch(
                        _browser_state["headless"],
                    ),
                )
                _browser_state["_sync_playwright"] = pw
                _browser_state["_sync_browser"] = browser
                _browser_state["browser_kind"] = kind
            else:
                async_playwright = _ensure_playwright_async()
                pw = await async_playwright().start()
                use_default = not is_running_in_container() and os.environ.get(
                    "COPAW_BROWSER_USE_DEFAULT",
                    "1",
                ).strip().lower() in ("1", "true", "yes")
                default_kind, default_path = (
                    get_system_default_browser()
                    if use_default
                    else (None, None)
                )
                exe: Optional[str] = None
                if default_kind == "chromium" and default_path:
                    exe = default_path
                elif default_kind != "webkit":
                    exe = _chromium_executable_path()
                kind = "chromium"
                if exe:
                    launch_kwargs: dict[str, Any] = {
                        "headless": _browser_state["headless"],
                    }
                    extra_args = _chromium_launch_args()
                    if extra_args:
                        launch_kwargs["args"] = extra_args
                    launch_kwargs["executable_path"] = exe
                    pw_browser = await pw.chromium.launch(
                        **launch_kwargs,
                    )
                elif default_kind == "webkit" or sys.platform == "darwin":
                    pw_browser = await pw.webkit.launch(
                        headless=_browser_state["headless"],
                    )
                    kind = "webkit"
                else:
                    launch_kwargs = {
                        "headless": _browser_state["headless"],
                    }
                    extra_args = _chromium_launch_args()
                    if extra_args:
                        launch_kwargs["args"] = extra_args
                    pw_browser = await pw.chromium.launch(
                        **launch_kwargs,
                    )
                _browser_state["playwright"] = pw
                _browser_state["browser"] = pw_browser
                _browser_state["browser_kind"] = kind
            _browser_state["_last_browser_error"] = None
            _start_idle_watchdog()
        except Exception as e:
            _browser_state["_last_browser_error"] = str(e)
            return False

    # Phase 2: ensure agent has a BrowserContext
    has_context = (
        ctx._sync_context is not None
        if _USE_SYNC_PLAYWRIGHT
        else ctx.context is not None
    )
    if not has_context:
        # Check for auto-saved storage state from previous session
        state_path = _storage_state_path(
            agent_id if agent_id else _get_agent_id(),
        )
        has_saved_state = os.path.isfile(state_path)
        try:
            if _USE_SYNC_PLAYWRIGHT:
                loop = asyncio.get_event_loop()
                browser = _browser_state["_sync_browser"]
                if has_saved_state:
                    context = await loop.run_in_executor(
                        _get_executor(),
                        lambda: browser.new_context(
                            storage_state=state_path,
                        ),
                    )
                    logger.info(
                        "Auto-loaded storage state for agent '%s'"
                        " from %s",
                        agent_id,
                        state_path,
                    )
                else:
                    context = await loop.run_in_executor(
                        _get_executor(),
                        browser.new_context,
                    )
                ctx._sync_context = context
            else:
                browser = _browser_state["browser"]
                if has_saved_state:
                    context = await browser.new_context(
                        storage_state=state_path,
                    )
                    logger.info(
                        "Auto-loaded storage state for agent '%s'"
                        " from %s",
                        agent_id,
                        state_path,
                    )
                else:
                    context = await browser.new_context()
                ctx.context = context
            _attach_context_listeners(context, ctx)
        except Exception as e:
            _browser_state["_last_browser_error"] = str(e)
            return False

        # Store saved sessionStorage data on ctx for lazy restore
        if has_saved_state:
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                ss_data = saved.get("sessionStorage", [])
                if ss_data:
                    ctx._pending_session_storage = ss_data
            except Exception:
                pass

    _touch_activity(agent_id)
    return True


async def _restore_session_storage(
    page: Any,
    ctx: AgentBrowserContext,
) -> None:
    """Restore sessionStorage to a page if pending data exists."""
    ss_data = getattr(ctx, "_pending_session_storage", None)
    if not ss_data:
        return
    try:
        # Get the page's origin
        if _USE_SYNC_PLAYWRIGHT:
            origin = await _run_sync(
                page.evaluate, "location.origin",
            )
        else:
            origin = await page.evaluate("location.origin")
        # Find matching sessionStorage data
        for entry in ss_data:
            if entry.get("origin") == origin:
                js = (
                    "(data) => {"
                    "  for (const [k, v] of Object.entries(data)) {"
                    "    sessionStorage.setItem(k, v);"
                    "  }"
                    "}"
                )
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(
                        page.evaluate, js, entry["data"],
                    )
                else:
                    await page.evaluate(js, entry["data"])
                logger.info(
                    "Restored sessionStorage for origin '%s'"
                    " (%d items)",
                    origin,
                    len(entry["data"]),
                )
                break
    except Exception as e:
        logger.warning("Failed to restore sessionStorage: %s", e)


async def ensure_browser_for_agent(agent_id: str) -> bool:
    """Public: ensure browser and agent context are ready.

    Can be called from outside the agent execution context
    (e.g. the live-view router) with an explicit *agent_id*.
    """
    return await _ensure_browser(agent_id=agent_id)


def _start_idle_watchdog() -> None:
    """Cancel any existing idle watchdog and start a fresh one."""
    old_task = _browser_state.get("_idle_task")
    if old_task and not old_task.done():
        old_task.cancel()
    _browser_state["_idle_task"] = asyncio.ensure_future(
        _idle_watchdog(),
    )


def _cancel_idle_watchdog() -> None:
    """Cancel the idle watchdog, if running."""
    task = _browser_state.get("_idle_task")
    if task and not task.done():
        task.cancel()
    _browser_state["_idle_task"] = None


# pylint: disable=R0912,R0915
async def _action_start(
    headed: bool = False,
) -> ToolResponse:
    # Force headless — Live View panel replaces visible window.
    headed = False
    _browser_state["headless"] = not headed

    ctx = _get_or_create_agent_ctx()
    # Check if this agent already has a context
    has_context = (
        ctx._sync_context is not None
        if _USE_SYNC_PLAYWRIGHT
        else ctx.context is not None
    )
    if has_context:
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Browser already running"},
                ensure_ascii=False,
                indent=2,
            ),
        )

    try:
        ok = await _ensure_browser()
        if not ok:
            err = (
                _browser_state.get("_last_browser_error")
                or "Browser start failed"
            )
            return _tool_response(
                json.dumps(
                    {"ok": False, "error": err},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        _touch_activity()
        await _notify_lifecycle(
            "started",
            agent_id=ctx.agent_id,
        )
        msg = (
            "Browser started (visible window)"
            if not _browser_state["headless"]
            else "Browser started"
        )
        return _tool_response(
            json.dumps(
                {"ok": True, "message": msg},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Browser start failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_stop() -> ToolResponse:
    agent_id = _get_agent_id()
    ctx = _get_agent_ctx(agent_id)

    if ctx is None:
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Browser not running"},
                ensure_ascii=False,
                indent=2,
            ),
        )

    # Step 1: close this agent's context (auto-saves storage state)
    try:
        await _async_close_agent_context(agent_id)
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Browser stop failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )

    await _notify_lifecycle("stopped", agent_id=agent_id)

    # Step 2: if no agent contexts remain, stop shared browser
    if not _agent_contexts:
        _cancel_idle_watchdog()
        try:
            await _async_stop_browser_process()
        except Exception:
            pass

    return _tool_response(
        json.dumps(
            {"ok": True, "message": "Browser stopped"},
            ensure_ascii=False,
            indent=2,
        ),
    )


async def _action_open(url: str, page_id: str) -> ToolResponse:
    url = (url or "").strip()
    if not url:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "url required for open"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    if not await _ensure_browser():
        err = (
            _browser_state.get("_last_browser_error") or "Browser not started"
        )
        return _tool_response(
            json.dumps(
                {"ok": False, "error": err},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_or_create_agent_ctx()

    # Reuse existing page if page_id already exists (preserves
    # sessionStorage and in-memory state like auth tokens).
    if page_id in ctx.pages and ctx.pages[page_id]:
        return await _action_navigate(url, page_id)

    try:
        ctx._creating_page = True
        try:
            if _USE_SYNC_PLAYWRIGHT:
                loop = asyncio.get_event_loop()
                page = await loop.run_in_executor(
                    _get_executor(),
                    lambda: ctx._sync_context.new_page(),
                )
            else:
                page = await ctx.context.new_page()
        finally:
            ctx._creating_page = False

        ctx.refs[page_id] = {}
        ctx.console_logs[page_id] = []
        ctx.network_requests[page_id] = []
        ctx.pending_dialogs[page_id] = []
        ctx.pending_file_choosers[page_id] = []
        _attach_page_listeners(page, page_id, ctx)

        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                _get_executor(),
                lambda: page.goto(url),
            )
        else:
            await page.goto(url)

        # Restore sessionStorage from previous session (if any)
        await _restore_session_storage(page, ctx)
        # If sessionStorage contained auth tokens, the page may need
        # a reload to pick them up (SPA init reads from storage).
        if getattr(ctx, "_pending_session_storage", None):
            actual_url = page.url
            # Only reload if page didn't redirect (still on target)
            if actual_url and actual_url.rstrip("/") == url.rstrip("/"):
                try:
                    if _USE_SYNC_PLAYWRIGHT:
                        await _run_sync(page.reload)
                    else:
                        await page.reload()
                except Exception:
                    pass
            ctx._pending_session_storage = []

        ctx.pages[page_id] = page
        ctx.current_page_id = page_id
        await _notify_lifecycle(
            "navigated",
            url=url,
            page_id=page_id,
            agent_id=ctx.agent_id,
        )
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Opened {url}",
                    "page_id": page_id,
                    "url": url,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Open failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_navigate(url: str, page_id: str) -> ToolResponse:
    url = (url or "").strip()
    if not url:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "url required for navigate"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    try:
        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                _get_executor(),
                lambda: page.goto(url),
            )
        else:
            await page.goto(url)
        if ctx:
            ctx.current_page_id = page_id
        await _notify_lifecycle(
            "navigated",
            url=page.url,
            page_id=page_id,
            agent_id=ctx.agent_id if ctx else "",
        )
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Navigated to {url}",
                    "url": page.url,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Navigate failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_screenshot(
    page_id: str,
    path: str,
    full_page: bool,
    screenshot_type: str = "png",
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    frame_selector: str = "",
) -> ToolResponse:
    path = (path or "").strip()
    if not path:
        ext = "jpeg" if screenshot_type == "jpeg" else "png"
        path = f"page-{int(time.time())}.{ext}"
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if ref and ref.strip():
            locator = _get_locator_by_ref(
                page,
                page_id,
                ref.strip(),
                frame_selector,
            )
            if locator is None:
                return _tool_response(
                    json.dumps(
                        {"ok": False, "error": f"Unknown ref: {ref}"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            if _USE_SYNC_PLAYWRIGHT:
                await _run_sync(
                    locator.screenshot,
                    path=path,
                    type=screenshot_type
                    if screenshot_type == "jpeg"
                    else "png",
                )
            else:
                await locator.screenshot(
                    path=path,
                    type=screenshot_type
                    if screenshot_type == "jpeg"
                    else "png",
                )
        else:
            if frame_selector and frame_selector.strip():
                root = _get_root(page, page_id, frame_selector)
                locator = root.locator("body").first
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(
                        locator.screenshot,
                        path=path,
                        type=screenshot_type
                        if screenshot_type == "jpeg"
                        else "png",
                    )
                else:
                    await locator.screenshot(
                        path=path,
                        type=screenshot_type
                        if screenshot_type == "jpeg"
                        else "png",
                    )
            else:
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(
                        page.screenshot,
                        path=path,
                        full_page=full_page,
                        type=screenshot_type
                        if screenshot_type == "jpeg"
                        else "png",
                    )
                else:
                    await page.screenshot(
                        path=path,
                        full_page=full_page,
                        type=screenshot_type
                        if screenshot_type == "jpeg"
                        else "png",
                    )
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Screenshot saved to {path}",
                    "path": path,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Screenshot failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_click(  # pylint: disable=too-many-branches
    page_id: str,
    selector: str,
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    wait: int = 0,
    double_click: bool = False,
    button: str = "left",
    modifiers_json: str = "",
    frame_selector: str = "",
) -> ToolResponse:
    ref = (ref or "").strip()
    selector = (selector or "").strip()
    if not ref and not selector:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "selector or ref required for click"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if wait > 0:
            await asyncio.sleep(wait / 1000.0)
        mods = _parse_json_param(modifiers_json, [])
        if not isinstance(mods, list):
            mods = []
        kwargs = {
            "button": button
            if button in ("left", "right", "middle")
            else "left",
        }
        if mods:
            kwargs["modifiers"] = [
                m
                for m in mods
                if m in ("Alt", "Control", "ControlOrMeta", "Meta", "Shift")
            ]

        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            if ref:
                locator = _get_locator_by_ref(
                    page,
                    page_id,
                    ref,
                    frame_selector,
                )
                if locator is None:
                    return _tool_response(
                        json.dumps(
                            {"ok": False, "error": f"Unknown ref: {ref}"},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                if double_click:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.dblclick(**kwargs),
                    )
                else:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.click(**kwargs),
                    )
            else:
                root = _get_root(page, page_id, frame_selector)
                locator = root.locator(selector).first
                if double_click:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.dblclick(**kwargs),
                    )
                else:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.click(**kwargs),
                    )
        else:
            # Standard async mode
            if ref:
                locator = _get_locator_by_ref(
                    page,
                    page_id,
                    ref,
                    frame_selector,
                )
                if locator is None:
                    return _tool_response(
                        json.dumps(
                            {"ok": False, "error": f"Unknown ref: {ref}"},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                if double_click:
                    await locator.dblclick(**kwargs)
                else:
                    await locator.click(**kwargs)
            else:
                root = _get_root(page, page_id, frame_selector)
                locator = root.locator(selector).first
                if double_click:
                    await locator.dblclick(**kwargs)
                else:
                    await locator.click(**kwargs)

        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Clicked {ref or selector}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Click failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_type(
    page_id: str,
    selector: str,
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    text: str = "",
    submit: bool = False,
    slowly: bool = False,
    frame_selector: str = "",
) -> ToolResponse:
    ref = (ref or "").strip()
    selector = (selector or "").strip()
    if not ref and not selector:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "selector or ref required for type"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if ref:
            locator = _get_locator_by_ref(page, page_id, ref, frame_selector)
            if locator is None:
                return _tool_response(
                    json.dumps(
                        {"ok": False, "error": f"Unknown ref: {ref}"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            if _USE_SYNC_PLAYWRIGHT:
                loop = asyncio.get_event_loop()
                if slowly:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.press_sequentially(text or ""),
                    )
                else:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.fill(text or ""),
                    )
                if submit:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: locator.press("Enter"),
                    )
            else:
                if slowly:
                    await locator.press_sequentially(text or "")
                else:
                    await locator.fill(text or "")
                if submit:
                    await locator.press("Enter")
        else:
            root = _get_root(page, page_id, frame_selector)
            loc = root.locator(selector).first
            if _USE_SYNC_PLAYWRIGHT:
                loop = asyncio.get_event_loop()
                if slowly:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: loc.press_sequentially(text or ""),
                    )
                else:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: loc.fill(text or ""),
                    )
                if submit:
                    await loop.run_in_executor(
                        _get_executor(),
                        lambda: loc.press("Enter"),
                    )
            else:
                if slowly:
                    await loc.press_sequentially(text or "")
                else:
                    await loc.fill(text or "")
                if submit:
                    await loc.press("Enter")
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Typed into {ref or selector}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Type failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_eval(page_id: str, code: str) -> ToolResponse:
    code = (code or "").strip()
    if not code:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "code required for eval"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if code.strip().startswith("(") or code.strip().startswith("function"):
            if _USE_SYNC_PLAYWRIGHT:
                result = await _run_sync(page.evaluate, code)
            else:
                result = await page.evaluate(code)
        else:
            if _USE_SYNC_PLAYWRIGHT:
                result = await _run_sync(
                    page.evaluate,
                    f"() => {{ return ({code}); }}",
                )
            else:
                result = await page.evaluate(f"() => {{ return ({code}); }}")
        try:
            out = json.dumps(
                {"ok": True, "result": result},
                ensure_ascii=False,
                indent=2,
            )
        except TypeError:
            out = json.dumps(
                {"ok": True, "result": str(result)},
                ensure_ascii=False,
                indent=2,
            )
        return _tool_response(out)
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Eval failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_pdf(page_id: str, path: str) -> ToolResponse:
    path = (path or "page.pdf").strip() or "page.pdf"
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(page.pdf, path=path)
        else:
            await page.pdf(path=path)
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"PDF saved to {path}", "path": path},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"PDF failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_close(page_id: str) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(page.close)
        else:
            await page.close()
        if ctx:
            ctx.pages.pop(page_id, None)
            ctx.refs.pop(page_id, None)
            ctx.refs_frame.pop(page_id, None)
            ctx.console_logs.pop(page_id, None)
            ctx.network_requests.pop(page_id, None)
            ctx.pending_dialogs.pop(page_id, None)
            ctx.pending_file_choosers.pop(page_id, None)
            if ctx.current_page_id == page_id:
                remaining = list(ctx.pages.keys())
                ctx.current_page_id = remaining[0] if remaining else None
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Closed page '{page_id}'"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Close failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_get_cookies(
    cookies_url: str = "",
    cookies_file: str = "",
) -> ToolResponse:
    """Export cookies from the current agent's BrowserContext."""
    ctx = _get_agent_ctx()
    if not ctx or not (ctx.context or ctx._sync_context):
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "Browser not started"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        context = ctx._sync_context if _USE_SYNC_PLAYWRIGHT else ctx.context
        if cookies_url:
            if _USE_SYNC_PLAYWRIGHT:
                cookies = await _run_sync(context.cookies, cookies_url)
            else:
                cookies = await context.cookies(cookies_url)
        else:
            if _USE_SYNC_PLAYWRIGHT:
                cookies = await _run_sync(context.cookies)
            else:
                cookies = await context.cookies()
        if cookies_file:
            cookies_file = os.path.expanduser(cookies_file)
            os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
            with open(cookies_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            return _tool_response(
                json.dumps(
                    {
                        "ok": True,
                        "count": len(cookies),
                        "file": cookies_file,
                        "message": (
                            f"Saved {len(cookies)} cookies to"
                            f" {cookies_file}"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "count": len(cookies),
                    "cookies": cookies,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Get cookies failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_set_cookies(
    cookies_json: str = "",
    cookies_file: str = "",
) -> ToolResponse:
    """Import cookies into the current agent's BrowserContext."""
    ctx = _get_agent_ctx()
    if not ctx or not (ctx.context or ctx._sync_context):
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "Browser not started"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    cookies = None
    if cookies_json:
        cookies = _parse_json_param(cookies_json)
        if not isinstance(cookies, list):
            return _tool_response(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "cookies_json must be a JSON array of cookie"
                            " objects"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    elif cookies_file:
        cookies_file = os.path.expanduser(cookies_file)
        if not os.path.isfile(cookies_file):
            return _tool_response(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"Cookie file not found: {cookies_file}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        try:
            with open(cookies_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception as e:
            return _tool_response(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"Failed to read cookie file: {e!s}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    if not cookies:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "Provide cookies_json or cookies_file"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        context = ctx._sync_context if _USE_SYNC_PLAYWRIGHT else ctx.context
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(context.add_cookies, cookies)
        else:
            await context.add_cookies(cookies)
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "count": len(cookies),
                    "message": f"Set {len(cookies)} cookies",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Set cookies failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_save_storage_state(
    cookies_file: str = "",
) -> ToolResponse:
    """Save full storage state (cookies + localStorage) to file."""
    ctx = _get_agent_ctx()
    if not ctx or not (ctx.context or ctx._sync_context):
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "Browser not started"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    if not cookies_file:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": "cookies_file required for save_storage_state",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        cookies_file = os.path.expanduser(cookies_file)
        os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
        context = ctx._sync_context if _USE_SYNC_PLAYWRIGHT else ctx.context
        if _USE_SYNC_PLAYWRIGHT:
            state = await _run_sync(context.storage_state)
        else:
            state = await context.storage_state()
        with open(cookies_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        n_cookies = len(state.get("cookies", []))
        n_origins = len(state.get("origins", []))
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "file": cookies_file,
                    "cookies_count": n_cookies,
                    "origins_count": n_origins,
                    "message": (
                        f"Saved storage state ({n_cookies} cookies,"
                        f" {n_origins} origins with localStorage)"
                        f" to {cookies_file}"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Save storage state failed: {e!s}",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_load_storage_state(
    cookies_file: str = "",
) -> ToolResponse:
    """Re-create agent BrowserContext with saved storage state."""
    if not cookies_file:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "cookies_file required for load_storage_state"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    cookies_file = os.path.expanduser(cookies_file)
    if not os.path.isfile(cookies_file):
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Storage state file not found: {cookies_file}",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    # Ensure browser process is running
    if not _is_browser_running():
        ok = await _ensure_browser()
        if not ok:
            return _tool_response(
                json.dumps(
                    {
                        "ok": False,
                        "error": "Failed to start browser",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    agent_id = _get_agent_id()
    # Close existing context for this agent (if any)
    ctx = _get_agent_ctx(agent_id)
    if ctx and (ctx.context or ctx._sync_context):
        for page in list(ctx.pages.values()):
            try:
                if _USE_SYNC_PLAYWRIGHT:
                    page.close()
                else:
                    await page.close()
            except Exception:
                pass
        ctx.pages.clear()
        ctx.refs.clear()
        ctx.refs_frame.clear()
        ctx.console_logs.clear()
        ctx.network_requests.clear()
        ctx.pending_dialogs.clear()
        ctx.pending_file_choosers.clear()
        ctx.current_page_id = None
        ctx.page_counter = 0
        if _USE_SYNC_PLAYWRIGHT and ctx._sync_context:
            try:
                ctx._sync_context.close()
            except Exception:
                pass
            ctx._sync_context = None
        elif ctx.context:
            try:
                await ctx.context.close()
            except Exception:
                pass
            ctx.context = None
    # Create new context with storage_state
    try:
        ctx = _get_or_create_agent_ctx(agent_id)
        if _USE_SYNC_PLAYWRIGHT:
            browser = _browser_state["_sync_browser"]
            loop = asyncio.get_event_loop()
            context = await loop.run_in_executor(
                _get_executor(),
                lambda: browser.new_context(
                    storage_state=cookies_file,
                ),
            )
            ctx._sync_context = context
        else:
            browser = _browser_state["browser"]
            context = await browser.new_context(
                storage_state=cookies_file,
            )
            ctx.context = context
        _attach_context_listeners(context, ctx)
        _touch_activity(agent_id)
        # Read state info for response
        with open(cookies_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        n_cookies = len(state.get("cookies", []))
        n_origins = len(state.get("origins", []))
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "file": cookies_file,
                    "cookies_count": n_cookies,
                    "origins_count": n_origins,
                    "message": (
                        f"Loaded storage state ({n_cookies} cookies,"
                        f" {n_origins} origins) from {cookies_file}."
                        f" Navigate to target URL now."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Load storage state failed: {e!s}",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_snapshot(
    page_id: str,
    filename: str,
    frame_selector: str = "",
) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if _USE_SYNC_PLAYWRIGHT:
            # Hybrid mode: execute in thread pool
            loop = asyncio.get_event_loop()
            root = _get_root(page, page_id, frame_selector)
            locator = root.locator(":root")
            raw = await loop.run_in_executor(
                _get_executor(),
                lambda: locator.aria_snapshot(),  # pylint: disable=unnecessary-lambda
            )
        else:
            root = _get_root(page, page_id, frame_selector)
            locator = root.locator(":root")
            raw = await locator.aria_snapshot()

        raw_str = str(raw) if raw is not None else ""
        snapshot, refs = build_role_snapshot_from_aria(
            raw_str,
            interactive=False,
            compact=False,
        )
        ctx = _get_agent_ctx()
        if ctx:
            ctx.refs[page_id] = refs
            ctx.refs_frame[page_id] = (
                frame_selector.strip() if frame_selector else ""
            )
        out = {
            "ok": True,
            "snapshot": snapshot,
            "refs": list(refs.keys()),
            "url": page.url,
        }
        if frame_selector and frame_selector.strip():
            out["frame_selector"] = frame_selector.strip()
        if filename and filename.strip():
            with open(filename.strip(), "w", encoding="utf-8") as f:
                f.write(snapshot)
            out["filename"] = filename.strip()
        return _tool_response(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Snapshot failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_navigate_back(page_id: str) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(page.go_back)
        else:
            await page.go_back()
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Navigated back", "url": page.url},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Navigate back failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_evaluate(
    page_id: str,
    code: str,
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    frame_selector: str = "",
) -> ToolResponse:
    code = (code or "").strip()
    if not code:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "code required for evaluate"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if ref and ref.strip():
            locator = _get_locator_by_ref(
                page,
                page_id,
                ref.strip(),
                frame_selector,
            )
            if locator is None:
                return _tool_response(
                    json.dumps(
                        {"ok": False, "error": f"Unknown ref: {ref}"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            if _USE_SYNC_PLAYWRIGHT:
                result = await _run_sync(locator.evaluate, code)
            else:
                result = await locator.evaluate(code)
        else:
            if code.strip().startswith("(") or code.strip().startswith(
                "function",
            ):
                if _USE_SYNC_PLAYWRIGHT:
                    result = await _run_sync(page.evaluate, code)
                else:
                    result = await page.evaluate(code)
            else:
                if _USE_SYNC_PLAYWRIGHT:
                    result = await _run_sync(
                        page.evaluate,
                        f"() => {{ return ({code}); }}",
                    )
                else:
                    result = await page.evaluate(
                        f"() => {{ return ({code}); }}",
                    )
        try:
            out = json.dumps(
                {"ok": True, "result": result},
                ensure_ascii=False,
                indent=2,
            )
        except TypeError:
            out = json.dumps(
                {"ok": True, "result": str(result)},
                ensure_ascii=False,
                indent=2,
            )
        return _tool_response(out)
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Evaluate failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_resize(
    page_id: str,
    width: int,
    height: int,
) -> ToolResponse:
    if width <= 0 or height <= 0:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "width and height must be positive"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(
                page.set_viewport_size,
                {"width": width, "height": height},
            )
        else:
            await page.set_viewport_size({"width": width, "height": height})
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Resized to {width}x{height}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Resize failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_console_messages(
    page_id: str,
    level: str,
    filename: str,
) -> ToolResponse:
    level = (level or "info").strip().lower()
    order = ("error", "warning", "info", "debug")
    idx = order.index(level) if level in order else 2
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    logs = ctx.console_logs.get(page_id, []) if ctx else []
    filtered = (
        [m for m in logs if order.index(m["level"]) <= idx]
        if level in order
        else logs
    )
    lines = [f"[{m['level']}] {m['text']}" for m in filtered]
    text = "\n".join(lines)
    if filename and filename.strip():
        with open(filename.strip(), "w", encoding="utf-8") as f:
            f.write(text)
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Console messages saved to {filename}",
                    "filename": filename.strip(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return _tool_response(
        json.dumps(
            {"ok": True, "messages": filtered, "text": text},
            ensure_ascii=False,
            indent=2,
        ),
    )


async def _action_handle_dialog(
    page_id: str,
    accept: bool,
    prompt_text: str,
) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    dialogs = ctx.pending_dialogs.get(page_id, []) if ctx else []
    if not dialogs:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "No pending dialog"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        dialog = dialogs.pop(0)
        if accept:
            if prompt_text and hasattr(dialog, "accept"):
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(dialog.accept, prompt_text)
                else:
                    await dialog.accept(prompt_text)
            else:
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(dialog.accept)
                else:
                    await dialog.accept()
        else:
            if _USE_SYNC_PLAYWRIGHT:
                await _run_sync(dialog.dismiss)
            else:
                await dialog.dismiss()
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Dialog handled"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Handle dialog failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_file_upload(page_id: str, paths_json: str) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    paths = _parse_json_param(paths_json, [])
    if not isinstance(paths, list):
        paths = []
    try:
        ctx = _get_agent_ctx()
        choosers = ctx.pending_file_choosers.get(page_id, []) if ctx else []
        if not choosers:
            return _tool_response(
                json.dumps(
                    {
                        "ok": False,
                        "error": "No chooser. Click upload then file_upload.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        chooser = choosers.pop(0)
        if paths:
            if _USE_SYNC_PLAYWRIGHT:
                await _run_sync(chooser.set_files, paths)
            else:
                await chooser.set_files(paths)
            return _tool_response(
                json.dumps(
                    {"ok": True, "message": f"Uploaded {len(paths)} file(s)"},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(chooser.set_files, [])
        else:
            await chooser.set_files([])
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "File chooser cancelled"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"File upload failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_fill_form(page_id: str, fields_json: str) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    fields = _parse_json_param(fields_json, [])
    if not isinstance(fields, list) or not fields:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "fields required (JSON array)"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    refs = _get_refs(page_id)
    # Use last snapshot's frame so fill_form works after iframe snapshot
    ctx = _get_agent_ctx()
    frame = ctx.refs_frame.get(page_id, "") if ctx else ""
    try:
        for f in fields:
            ref = (f.get("ref") or "").strip()
            if not ref or ref not in refs:
                continue
            locator = _get_locator_by_ref(page, page_id, ref, frame)
            if locator is None:
                continue
            field_type = (f.get("type") or "textbox").lower()
            value = f.get("value")
            if field_type == "checkbox":
                if isinstance(value, str):
                    value = value.strip().lower() in ("true", "1", "yes")
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(locator.set_checked, bool(value))
                else:
                    await locator.set_checked(bool(value))
            elif field_type == "radio":
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(locator.set_checked, True)
                else:
                    await locator.set_checked(True)
            elif field_type == "combobox":
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(
                        locator.select_option,
                        label=value if isinstance(value, str) else None,
                        value=value,
                    )
                else:
                    await locator.select_option(
                        label=value if isinstance(value, str) else None,
                        value=value,
                    )
            elif field_type == "slider":
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(locator.fill, str(value))
                else:
                    await locator.fill(str(value))
            else:
                if _USE_SYNC_PLAYWRIGHT:
                    await _run_sync(
                        locator.fill,
                        str(value) if value is not None else "",
                    )
                else:
                    await locator.fill(str(value) if value is not None else "")
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Filled {len(fields)} field(s)"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Fill form failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


def _run_playwright_install() -> None:
    """Run playwright install in a blocking way (for use in thread)."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install"],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,  # 10 minutes max
    )


async def _action_install() -> ToolResponse:
    """Install Playwright browsers. If a system Chrome/Chromium/Edge is found,
    use it and skip download. On macOS with no Chromium, use Safari (WebKit)
    so no download is needed. Only run playwright install when necessary.
    """
    exe = _chromium_executable_path()
    if exe:
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Using system browser (no download): {exe}",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    if _use_webkit_fallback():
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": "On macOS using Safari (WebKit); no browser download needed.",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        await asyncio.to_thread(_run_playwright_install)
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Browser installed"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except subprocess.TimeoutExpired:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": "Browser install timed out (10 min). Run manually in terminal: "
                    f"{sys.executable!s} -m playwright install",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Install failed: {e!s}. Install manually: "
                    f"{sys.executable!s} -m pip install playwright && "
                    f"{sys.executable!s} -m playwright install",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_press_key(page_id: str, key: str) -> ToolResponse:
    key = (key or "").strip()
    if not key:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "key required for press_key"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(page.keyboard.press, key)
        else:
            await page.keyboard.press(key)
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Pressed key {key}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Press key failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_network_requests(
    page_id: str,
    include_static: bool,
    filename: str,
) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    requests = ctx.network_requests.get(page_id, []) if ctx else []
    if not include_static:
        static = ("image", "stylesheet", "font", "media")
        requests = [r for r in requests if r.get("resourceType") not in static]
    lines = [
        f"{r.get('method', '')} {r.get('url', '')} {r.get('status', '')}"
        for r in requests
    ]
    text = "\n".join(lines)
    if filename and filename.strip():
        with open(filename.strip(), "w", encoding="utf-8") as f:
            f.write(text)
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Network requests saved to {filename}",
                    "filename": filename.strip(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return _tool_response(
        json.dumps(
            {"ok": True, "requests": requests, "text": text},
            ensure_ascii=False,
            indent=2,
        ),
    )


async def _action_run_code(page_id: str, code: str) -> ToolResponse:
    """Run JS in page (like eval). Use evaluate for element (ref)."""
    code = (code or "").strip()
    if not code:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "code required for run_code"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if code.strip().startswith("(") or code.strip().startswith("function"):
            if _USE_SYNC_PLAYWRIGHT:
                result = await _run_sync(page.evaluate, code)
            else:
                result = await page.evaluate(code)
        else:
            if _USE_SYNC_PLAYWRIGHT:
                result = await _run_sync(
                    page.evaluate,
                    f"() => {{ return ({code}); }}",
                )
            else:
                result = await page.evaluate(f"() => {{ return ({code}); }}")
        try:
            out = json.dumps(
                {"ok": True, "result": result},
                ensure_ascii=False,
                indent=2,
            )
        except TypeError:
            out = json.dumps(
                {"ok": True, "result": str(result)},
                ensure_ascii=False,
                indent=2,
            )
        return _tool_response(out)
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Run code failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_drag(
    page_id: str,
    start_ref: str,
    end_ref: str,
    start_selector: str = "",
    end_selector: str = "",
    start_element: str = "",  # pylint: disable=unused-argument
    end_element: str = "",  # pylint: disable=unused-argument
    frame_selector: str = "",
) -> ToolResponse:
    start_ref = (start_ref or "").strip()
    end_ref = (end_ref or "").strip()
    start_selector = (start_selector or "").strip()
    end_selector = (end_selector or "").strip()
    use_refs = bool(start_ref and end_ref)
    use_selectors = bool(start_selector and end_selector)
    if not use_refs and not use_selectors:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "drag needs (start_ref,end_ref) or (start_sel,end_sel)"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        root = _get_root(page, page_id, frame_selector)
        if use_refs:
            start_locator = _get_locator_by_ref(
                page,
                page_id,
                start_ref,
                frame_selector,
            )
            end_locator = _get_locator_by_ref(
                page,
                page_id,
                end_ref,
                frame_selector,
            )
            if start_locator is None or end_locator is None:
                return _tool_response(
                    json.dumps(
                        {"ok": False, "error": "Unknown ref for drag"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        else:
            start_locator = root.locator(start_selector).first
            end_locator = root.locator(end_selector).first
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(start_locator.drag_to, end_locator)
        else:
            await start_locator.drag_to(end_locator)
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Drag completed"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Drag failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_hover(
    page_id: str,
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    selector: str = "",
    frame_selector: str = "",
) -> ToolResponse:
    ref = (ref or "").strip()
    selector = (selector or "").strip()
    if not ref and not selector:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "hover requires ref or selector"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if ref:
            locator = _get_locator_by_ref(page, page_id, ref, frame_selector)
            if locator is None:
                return _tool_response(
                    json.dumps(
                        {"ok": False, "error": f"Unknown ref: {ref}"},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        else:
            root = _get_root(page, page_id, frame_selector)
            locator = root.locator(selector).first
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(locator.hover)
        else:
            await locator.hover()
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Hovered {ref or selector}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Hover failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_select_option(
    page_id: str,
    ref: str = "",
    element: str = "",  # pylint: disable=unused-argument
    values_json: str = "",
    frame_selector: str = "",
) -> ToolResponse:
    ref = (ref or "").strip()
    values = _parse_json_param(values_json, [])
    if not isinstance(values, list):
        values = [values] if values is not None else []
    if not ref:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": "ref required for select_option"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    if not values:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": "values required (JSON array or comma-separated)",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        locator = _get_locator_by_ref(page, page_id, ref, frame_selector)
        if locator is None:
            return _tool_response(
                json.dumps(
                    {"ok": False, "error": f"Unknown ref: {ref}"},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if _USE_SYNC_PLAYWRIGHT:
            await _run_sync(locator.select_option, value=values)
        else:
            await locator.select_option(value=values)
        return _tool_response(
            json.dumps(
                {"ok": True, "message": f"Selected {values}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Select option failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )


async def _action_tabs(  # pylint: disable=too-many-return-statements
    page_id: str,
    tab_action: str,
    index: int,
) -> ToolResponse:
    tab_action = (tab_action or "").strip().lower()
    if not tab_action:
        return _tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": "tab_action required (list, new, close, select)",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ctx = _get_agent_ctx()
    agent_pages = ctx.pages if ctx else {}
    page_ids = list(agent_pages.keys())
    if tab_action == "list":
        return _tool_response(
            json.dumps(
                {"ok": True, "tabs": page_ids, "count": len(page_ids)},
                ensure_ascii=False,
                indent=2,
            ),
        )
    if tab_action == "new":
        ok = await _ensure_browser()
        if not ok:
            err = (
                _browser_state.get("_last_browser_error")
                or "Browser not started"
            )
            return _tool_response(
                json.dumps(
                    {"ok": False, "error": err},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ctx = _get_or_create_agent_ctx()
        try:
            ctx._creating_page = True
            try:
                if _USE_SYNC_PLAYWRIGHT:
                    page = await _run_sync(
                        ctx._sync_context.new_page,
                    )
                else:
                    page = await ctx.context.new_page()
            finally:
                ctx._creating_page = False
            new_id = _next_page_id(ctx)
            ctx.refs[new_id] = {}
            ctx.console_logs[new_id] = []
            ctx.network_requests[new_id] = []
            ctx.pending_dialogs[new_id] = []
            _attach_page_listeners(page, new_id, ctx)
            ctx.pages[new_id] = page
            ctx.current_page_id = new_id
            return _tool_response(
                json.dumps(
                    {
                        "ok": True,
                        "page_id": new_id,
                        "tabs": list(ctx.pages.keys()),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as e:
            return _tool_response(
                json.dumps(
                    {"ok": False, "error": f"New tab failed: {e!s}"},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    if tab_action == "close":
        target_id = page_ids[index] if 0 <= index < len(page_ids) else page_id
        return await _action_close(target_id)
    if tab_action == "select":
        target_id = page_ids[index] if 0 <= index < len(page_ids) else page_id
        if ctx:
            ctx.current_page_id = target_id
        return _tool_response(
            json.dumps(
                {
                    "ok": True,
                    "message": f"Use page_id={target_id} for later actions",
                    "page_id": target_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return _tool_response(
        json.dumps(
            {"ok": False, "error": f"Unknown tab_action: {tab_action}"},
            ensure_ascii=False,
            indent=2,
        ),
    )


async def _action_wait_for(
    page_id: str,
    wait_time: float,
    text: str,
    text_gone: str,
) -> ToolResponse:
    page = _get_page(page_id)
    if not page:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Page '{page_id}' not found"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    try:
        if wait_time and wait_time > 0:
            await asyncio.sleep(wait_time)
        text = (text or "").strip()
        text_gone = (text_gone or "").strip()
        if text:
            locator = page.get_by_text(text)
            if _USE_SYNC_PLAYWRIGHT:
                await _run_sync(
                    locator.wait_for,
                    state="visible",
                    timeout=30000,
                )
            else:
                await locator.wait_for(
                    state="visible",
                    timeout=30000,
                )
        if text_gone:
            locator = page.get_by_text(text_gone)
            if _USE_SYNC_PLAYWRIGHT:
                await _run_sync(
                    locator.wait_for,
                    state="hidden",
                    timeout=30000,
                )
            else:
                await locator.wait_for(
                    state="hidden",
                    timeout=30000,
                )
        return _tool_response(
            json.dumps(
                {"ok": True, "message": "Wait completed"},
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as e:
        return _tool_response(
            json.dumps(
                {"ok": False, "error": f"Wait failed: {e!s}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
