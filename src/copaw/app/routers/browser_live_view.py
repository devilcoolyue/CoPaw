# -*- coding: utf-8 -*-
"""Browser live-view router.

Provides a WebSocket endpoint that streams live browser frames to
connected front-end clients and relays mouse/keyboard input back to
the browser page.

**Dual capture mode:**

* **Chromium** — uses CDP ``Page.startScreencast`` (event-driven,
  frames are pushed only when the page content changes).
* **WebKit / fallback** — periodic ``page.screenshot()`` at ~5 fps.

Additionally exposes ``GET /browser/status`` for polling the browser
state without opening a WebSocket.

Each WebSocket connection is scoped to an ``agent_id`` (query param,
defaults to ``"default"``).  Screenshots and input are routed to the
corresponding agent's browser context.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ...agents.tools.browser_control import (
    close_tab_by_id,
    create_new_tab,
    ensure_browser_for_agent,
    get_browser_kind,
    get_browser_state_summary,
    get_browser_tabs,
    get_page,
    is_browser_running,
    register_browser_lifecycle_callback,
    set_current_page,
    touch_activity,
    _USE_SYNC_PLAYWRIGHT,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_JPEG_QUALITY = 65
_FRAME_INTERVAL = 0.2  # ~5 fps (screenshot fallback only)
_CDP_MIN_FRAME_INTERVAL = 0.1  # 100ms = ~10 fps cap for CDP
_WS_PING_INTERVAL = 15  # seconds between server pings

# ---------------------------------------------------------------------------
# Connected WebSocket clients (keyed by agent_id)
# ---------------------------------------------------------------------------
_ws_clients: dict[str, set[WebSocket]] = {}
_screencaster_task: asyncio.Task | None = None

# ---------------------------------------------------------------------------
# CDP Screencast state (keyed by agent_id)
# ---------------------------------------------------------------------------
_cdp_sessions: dict[str, Any] = {}  # agent_id -> CDPSession
_cdp_pages: dict[str, Any] = {}  # agent_id -> page when CDP started
_cdp_sending: dict[str, bool] = {}  # agent_id -> broadcast in progress
_cdp_last_frame_time: dict[str, float] = {}  # agent_id -> monotonic ts

# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


@router.get("/status")
async def browser_status(
    agent_id: str = Query("default"),
):
    """Return current browser state for a specific agent."""
    return get_browser_state_summary(agent_id=agent_id)


@router.get("/tabs")
async def browser_tabs(
    agent_id: str = Query("default"),
):
    """Return the list of open tabs for a specific agent."""
    return await get_browser_tabs(agent_id=agent_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_any_clients() -> bool:
    """Check if any agent_id has connected WS clients."""
    return any(clients for clients in _ws_clients.values())


def _get_clients(agent_id: str) -> set[WebSocket]:
    """Return the WS client set for an agent_id."""
    return _ws_clients.setdefault(agent_id, set())


def _is_cdp_active(agent_id: str) -> bool:
    """True if CDP screencast is running for *agent_id*."""
    return agent_id in _cdp_sessions


def _should_use_cdp() -> bool:
    """True when CDP screencast can be used (Chromium + async)."""
    return not _USE_SYNC_PLAYWRIGHT and get_browser_kind() == "chromium"


# Last broadcast tab fingerprints (detect structural changes)
_last_tab_snapshots: dict[str, str] = {}


def _tab_fingerprint(tabs: list[dict]) -> str:
    """Stable fingerprint for change detection: page ids + active."""
    return "|".join(
        f"{t['page_id']}:{'A' if t['active'] else '-'}" for t in tabs
    )


async def _broadcast_tabs(
    agent_id: str,
    force: bool = False,
) -> None:
    """Send the current tab list to all WS clients for *agent_id*.

    Uses a lightweight fingerprint (page_ids + active flag) so that
    volatile fields like title/url don't cause constant re-sends.
    Pass *force=True* to always send regardless of fingerprint.
    """
    tabs = await get_browser_tabs(agent_id=agent_id)
    fp = _tab_fingerprint(tabs)
    if not force and fp == _last_tab_snapshots.get(agent_id):
        return
    _last_tab_snapshots[agent_id] = fp
    msg = json.dumps({"type": "tabs", "tabs": tabs})
    await _broadcast_to_agent(agent_id, text=msg)


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


async def _broadcast_to_agent(
    agent_id: str,
    text: str | None = None,
    data: bytes | None = None,
) -> None:
    """Send text and/or bytes to all WS clients for an agent_id."""
    clients = _ws_clients.get(agent_id)
    if not clients:
        return
    dead: list[WebSocket] = []
    for ws in list(clients):
        try:
            if text is not None:
                await ws.send_text(text)
            if data is not None:
                await ws.send_bytes(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# ---------------------------------------------------------------------------
# CDP Screencast (Chromium only)
# ---------------------------------------------------------------------------


async def _start_cdp_screencast(agent_id: str) -> bool:
    """Start CDP screencast for a Chromium browser.  Returns True on
    success, False if it cannot be started (wrong engine, no page, etc.).
    """
    if _is_cdp_active(agent_id):
        return True  # already running
    page = get_page(agent_id=agent_id)
    if page is None:
        return False

    vp_w, vp_h = 1280, 720
    try:
        vp = page.viewport_size
        if vp:
            vp_w = vp.get("width", 1280)
            vp_h = vp.get("height", 720)
    except Exception:
        pass

    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception:
        logger.debug(
            "CDP session creation failed for agent %s, "
            "falling back to screenshot",
            agent_id,
            exc_info=True,
        )
        return False

    last_url: dict[str, str] = {"value": ""}
    _cdp_sending[agent_id] = False
    _cdp_last_frame_time[agent_id] = 0.0

    async def _send_frame(
        metadata: str,
        jpeg_bytes: bytes,
        session_id: int,
    ) -> None:
        """Broadcast one frame with backpressure tracking."""
        try:
            await _broadcast_to_agent(
                agent_id,
                text=metadata,
                data=jpeg_bytes,
            )
        finally:
            _cdp_sending[agent_id] = False
            # ACK after broadcast so CDP respects our pace
            if agent_id in _cdp_sessions:
                try:
                    await cdp.send(
                        "Page.screencastFrameAck",
                        {"sessionId": session_id},
                    )
                except Exception:
                    pass

    def _on_frame(params: dict) -> None:
        """Handle a CDP screencastFrame event."""
        # Guard: skip if CDP session already torn down
        if agent_id not in _cdp_sessions:
            return
        # Backpressure: skip frame if previous send in progress
        if _cdp_sending.get(agent_id):
            return
        # Throttle: enforce minimum interval between frames
        now = time.monotonic()
        elapsed = now - _cdp_last_frame_time.get(agent_id, 0.0)
        if elapsed < _CDP_MIN_FRAME_INTERVAL:
            # ACK immediately so CDP keeps running, but drop frame
            asyncio.ensure_future(
                cdp.send(
                    "Page.screencastFrameAck",
                    {"sessionId": params["sessionId"]},
                ),
            )
            return

        try:
            _cdp_sending[agent_id] = True
            _cdp_last_frame_time[agent_id] = now

            jpeg_bytes = base64.b64decode(params["data"])
            meta = params.get("metadata", {})
            w = meta.get("deviceWidth", vp_w)
            h = meta.get("deviceHeight", vp_h)
            metadata = json.dumps(
                {
                    "type": "frame",
                    "ts": int(time.time() * 1000),
                    "w": w,
                    "h": h,
                },
            )

            # Detect URL changes
            try:
                cur_url = page.url
                if cur_url and cur_url != last_url["value"]:
                    last_url["value"] = cur_url
                    nav_msg = json.dumps(
                        {
                            "type": "navigation",
                            "url": cur_url,
                            "page_id": "",
                        },
                    )
                    asyncio.ensure_future(
                        _broadcast_to_agent(
                            agent_id,
                            text=nav_msg,
                        ),
                    )
            except Exception:
                pass

            asyncio.ensure_future(
                _send_frame(
                    metadata,
                    jpeg_bytes,
                    params["sessionId"],
                ),
            )
        except Exception:
            _cdp_sending[agent_id] = False
            logger.debug(
                "CDP frame handler error",
                exc_info=True,
            )

    cdp.on("Page.screencastFrame", _on_frame)

    try:
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": _JPEG_QUALITY,
                "maxWidth": vp_w,
                "maxHeight": vp_h,
            },
        )
    except Exception:
        logger.debug(
            "CDP startScreencast failed for agent %s",
            agent_id,
            exc_info=True,
        )
        try:
            await cdp.detach()
        except Exception:
            pass
        return False

    _cdp_sessions[agent_id] = cdp
    _cdp_pages[agent_id] = page
    logger.debug(
        "CDP screencast started for agent %s (%dx%d q=%d)",
        agent_id,
        vp_w,
        vp_h,
        _JPEG_QUALITY,
    )
    return True


async def _stop_cdp_screencast(agent_id: str) -> None:
    """Stop CDP screencast for *agent_id* and detach the session."""
    cdp = _cdp_sessions.pop(agent_id, None)
    _cdp_pages.pop(agent_id, None)
    _cdp_sending.pop(agent_id, None)
    _cdp_last_frame_time.pop(agent_id, None)
    if cdp is None:
        return
    try:
        await cdp.send("Page.stopScreencast")
    except Exception:
        pass
    try:
        await cdp.detach()
    except Exception:
        pass
    logger.debug("CDP screencast stopped for agent %s", agent_id)


async def _stop_all_cdp_screencasts() -> None:
    """Stop all active CDP screencast sessions."""
    for aid in list(_cdp_sessions):
        await _stop_cdp_screencast(aid)


# ---------------------------------------------------------------------------
# Screenshot fallback loop (WebKit / sync Playwright)
# ---------------------------------------------------------------------------


async def _screenshot_one_agent(
    agent_id: str,
    last_urls: dict[str, str],
) -> None:
    """Take a single screenshot for *agent_id* and broadcast it."""
    page = get_page(agent_id=agent_id)
    if page is None:
        last_urls.pop(agent_id, None)
        return

    # Detect URL changes
    try:
        current_url = page.url
        prev_url = last_urls.get(agent_id, "")
        if current_url and current_url != prev_url:
            last_urls[agent_id] = current_url
            nav_msg = json.dumps(
                {
                    "type": "navigation",
                    "url": current_url,
                    "page_id": "",
                },
            )
            await _broadcast_to_agent(agent_id, text=nav_msg)
    except Exception:
        pass

    # Capture screenshot
    try:
        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            jpeg_bytes = await loop.run_in_executor(
                None,
                lambda p=page: p.screenshot(
                    type="jpeg",
                    quality=_JPEG_QUALITY,
                ),
            )
        else:
            jpeg_bytes = await page.screenshot(
                type="jpeg",
                quality=_JPEG_QUALITY,
            )
    except Exception:
        return

    # Viewport info
    vp_w, vp_h = 1280, 720
    try:
        vp = page.viewport_size
        if vp:
            vp_w = vp.get("width", 1280)
            vp_h = vp.get("height", 720)
    except Exception:
        pass

    metadata = json.dumps(
        {
            "type": "frame",
            "ts": int(time.time() * 1000),
            "w": vp_w,
            "h": vp_h,
        },
    )
    await _broadcast_to_agent(agent_id, text=metadata, data=jpeg_bytes)


async def _handle_cdp_agent(agent_id: str) -> None:
    """Maintain CDP screencast for an agent with an active session."""
    current_page = get_page(agent_id=agent_id)
    if current_page is not None and current_page is not _cdp_pages.get(
        agent_id,
    ):
        # Active page changed (e.g. new tab) — restart CDP
        await _stop_cdp_screencast(agent_id)
        await _start_cdp_screencast(agent_id)


async def _screencaster_loop() -> None:
    """Main capture loop: manages CDP and screenshot fallback."""
    global _screencaster_task
    last_urls: dict[str, str] = {}
    try:
        while _has_any_clients():
            if not is_browser_running():
                last_urls.clear()
                await asyncio.sleep(0.5)
                continue

            use_cdp = _should_use_cdp()

            for agent_id in list(_ws_clients.keys()):
                if not _ws_clients.get(agent_id):
                    continue

                if _is_cdp_active(agent_id):
                    await _handle_cdp_agent(agent_id)
                    continue

                if use_cdp:
                    if await _start_cdp_screencast(agent_id):
                        continue

                await _screenshot_one_agent(agent_id, last_urls)

            # Broadcast updated tab list (only sends when changed)
            for agent_id in list(_ws_clients.keys()):
                await _broadcast_tabs(agent_id)

            await asyncio.sleep(_FRAME_INTERVAL)
    except asyncio.CancelledError:
        pass
    finally:
        _screencaster_task = None


def _ensure_screencaster() -> None:
    """Start the screencaster loop if not already running."""
    global _screencaster_task
    if _screencaster_task is None or _screencaster_task.done():
        _screencaster_task = asyncio.ensure_future(
            _screencaster_loop(),
        )


def _stop_screencaster() -> None:
    """Cancel the screencaster if no clients remain."""
    global _screencaster_task
    if not _has_any_clients():
        asyncio.ensure_future(_stop_all_cdp_screencasts())
        if _screencaster_task and not _screencaster_task.done():
            _screencaster_task.cancel()
            _screencaster_task = None


# ---------------------------------------------------------------------------
# Lifecycle callback (registered with browser_control)
# ---------------------------------------------------------------------------


async def _on_browser_lifecycle(
    event: str,
    **kwargs: Any,
) -> None:
    """Broadcast browser lifecycle events to WS clients for the agent."""
    agent_id = kwargs.get("agent_id", "default")
    clients = _ws_clients.get(agent_id)
    if not clients:
        return

    if event == "started":
        summary = get_browser_state_summary(agent_id=agent_id)
        msg = json.dumps(
            {
                "type": "session",
                "status": "started",
                "viewport": summary.get("viewport", {}),
                "url": summary.get("url", ""),
            },
        )
    elif event == "stopped":
        await _stop_cdp_screencast(agent_id)
        msg = json.dumps(
            {
                "type": "session",
                "status": "stopped",
                "viewport": {},
                "url": "",
            },
        )
    elif event == "navigated":
        msg = json.dumps(
            {
                "type": "navigation",
                "url": kwargs.get("url", ""),
                "page_id": kwargs.get("page_id", ""),
            },
        )
    else:
        return

    await _broadcast_to_agent(agent_id, text=msg)
    # Broadcast updated tab list after any lifecycle event
    await _broadcast_tabs(agent_id, force=True)


# Register the callback at import time
register_browser_lifecycle_callback(_on_browser_lifecycle)

# ---------------------------------------------------------------------------
# Input handlers
# ---------------------------------------------------------------------------


async def _handle_mouse(
    data: dict,
    agent_id: str,
) -> None:
    """Relay mouse events to the browser page."""
    page = get_page(agent_id=agent_id)
    if not page:
        return

    action = data.get("action", "click")
    nx, ny = data.get("x", 0.5), data.get("y", 0.5)

    # Convert normalised coords -> actual viewport pixels
    vp_w, vp_h = 1280, 720
    try:
        vp = page.viewport_size
        if vp:
            vp_w = vp.get("width", 1280)
            vp_h = vp.get("height", 720)
    except Exception:
        pass

    x = nx * vp_w
    y = ny * vp_h
    btn = data.get("button", "left")

    try:
        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            if action == "click":
                await loop.run_in_executor(
                    None,
                    lambda: page.mouse.click(x, y, button=btn),
                )
            elif action == "dblclick":
                await loop.run_in_executor(
                    None,
                    lambda: page.mouse.dblclick(x, y, button=btn),
                )
            elif action == "move":
                await loop.run_in_executor(
                    None,
                    lambda: page.mouse.move(x, y),
                )
            elif action == "wheel":
                delta_y = data.get("deltaY", 0)
                await loop.run_in_executor(
                    None,
                    lambda: page.mouse.wheel(0, delta_y),
                )
        else:
            if action == "click":
                await page.mouse.click(x, y, button=btn)
            elif action == "dblclick":
                await page.mouse.dblclick(x, y, button=btn)
            elif action == "move":
                await page.mouse.move(x, y)
            elif action == "wheel":
                delta_y = data.get("deltaY", 0)
                await page.mouse.wheel(0, delta_y)
    except Exception as exc:
        logger.debug("Mouse action failed: %s", exc)

    touch_activity(agent_id=agent_id)


async def _handle_keyboard(
    data: dict,
    agent_id: str,
) -> None:
    """Relay keyboard events to the browser page."""
    page = get_page(agent_id=agent_id)
    if not page:
        return

    action = data.get("action", "press")
    key = data.get("key", "")
    text = data.get("text", "")

    try:
        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            if action == "type" and text:
                await loop.run_in_executor(
                    None,
                    lambda: page.keyboard.type(text),
                )
            elif key:
                await loop.run_in_executor(
                    None,
                    lambda: page.keyboard.press(key),
                )
        else:
            if action == "type" and text:
                await page.keyboard.type(text)
            elif key:
                await page.keyboard.press(key)
    except Exception as exc:
        logger.debug("Keyboard action failed: %s", exc)

    touch_activity(agent_id=agent_id)


async def _handle_navigate(
    data: dict,
    agent_id: str,
) -> None:
    """Handle navigation commands from the client."""
    page = get_page(agent_id=agent_id)
    if not page:
        # For URL navigation, auto-start browser and create a tab
        url = data.get("url", "")
        if data.get("type") == "navigate" and url:
            if not is_browser_running():
                ok = await ensure_browser_for_agent(agent_id)
                if not ok:
                    return
            result = await create_new_tab(agent_id=agent_id)
            if not result.get("ok"):
                return
            page = get_page(agent_id=agent_id)
            if not page:
                return
            if _is_cdp_active(agent_id):
                await _stop_cdp_screencast(agent_id)
                await _start_cdp_screencast(agent_id)
            await _broadcast_tabs(agent_id, force=True)
        else:
            return

    msg_type = data.get("type", "")

    try:
        if _USE_SYNC_PLAYWRIGHT:
            loop = asyncio.get_event_loop()
            if msg_type == "navigate":
                url = data.get("url", "")
                if url:
                    await loop.run_in_executor(
                        None,
                        lambda: page.goto(url),
                    )
            elif msg_type == "navigate_back":
                await loop.run_in_executor(
                    None,
                    lambda: page.go_back(),
                )
            elif msg_type == "navigate_forward":
                await loop.run_in_executor(
                    None,
                    lambda: page.go_forward(),
                )
            elif msg_type == "reload":
                await loop.run_in_executor(
                    None,
                    lambda: page.reload(),
                )
        else:
            if msg_type == "navigate":
                url = data.get("url", "")
                if url:
                    await page.goto(url)
            elif msg_type == "navigate_back":
                await page.go_back()
            elif msg_type == "navigate_forward":
                await page.go_forward()
            elif msg_type == "reload":
                await page.reload()
    except Exception as exc:
        logger.debug("Navigate action failed: %s", exc)

    touch_activity(agent_id=agent_id)


async def _handle_switch_tab(
    data: dict,
    agent_id: str,
) -> None:
    """Handle tab switch commands from the client."""
    page_id = data.get("page_id", "")
    if not page_id:
        return
    if not set_current_page(page_id, agent_id=agent_id):
        return
    # Restart CDP screencast for the new active page
    if _is_cdp_active(agent_id):
        await _stop_cdp_screencast(agent_id)
        await _start_cdp_screencast(agent_id)
    await _broadcast_tabs(agent_id)


async def _handle_new_tab(
    agent_id: str,
) -> None:
    """Create a new blank tab for the agent."""
    if not is_browser_running():
        ok = await ensure_browser_for_agent(agent_id)
        if not ok:
            return
    result = await create_new_tab(agent_id=agent_id)
    if result.get("ok"):
        # Restart CDP screencast for the new page
        if _is_cdp_active(agent_id):
            await _stop_cdp_screencast(agent_id)
            await _start_cdp_screencast(agent_id)
        await _broadcast_tabs(agent_id, force=True)
    touch_activity(agent_id=agent_id)


async def _handle_close_tab(
    data: dict,
    agent_id: str,
) -> None:
    """Close a specific tab for the agent."""
    page_id = data.get("page_id", "")
    if not page_id:
        return
    # Stop CDP if we're closing the active page
    if _is_cdp_active(agent_id):
        cdp_page = _cdp_pages.get(agent_id)
        closing_page = get_page(page_id, agent_id=agent_id)
        if cdp_page is closing_page:
            await _stop_cdp_screencast(agent_id)

    result = await close_tab_by_id(page_id, agent_id=agent_id)
    if result.get("ok"):
        # Restart CDP for the new active page if needed
        if _should_use_cdp() and not _is_cdp_active(agent_id):
            await _start_cdp_screencast(agent_id)
        await _broadcast_tabs(agent_id, force=True)
    touch_activity(agent_id=agent_id)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws")
async def browser_ws(
    websocket: WebSocket,
    agent_id: str = Query("default"),
) -> None:
    """Live browser view WebSocket.

    - Streams browser frames (text metadata + binary JPEG).
    - Accepts mouse / keyboard / navigate commands from the client.
    - Scoped to ``agent_id`` (query param, defaults to "default").
    """
    await websocket.accept()
    clients = _get_clients(agent_id)
    clients.add(websocket)

    # Send initial session state
    summary = get_browser_state_summary(agent_id=agent_id)
    status = "started" if summary["running"] else "stopped"
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "session",
                    "status": status,
                    "viewport": summary.get("viewport", {}),
                    "url": summary.get("url", ""),
                },
            ),
        )
    except Exception:
        clients.discard(websocket)
        return

    # Send initial tab list
    tabs = await get_browser_tabs(agent_id=agent_id)
    if tabs:
        try:
            await websocket.send_text(
                json.dumps({"type": "tabs", "tabs": tabs}),
            )
        except Exception:
            pass

    _ensure_screencaster()

    # Server-side ping to keep the connection alive
    async def _ping_loop() -> None:
        try:
            while True:
                await asyncio.sleep(_WS_PING_INTERVAL)
                try:
                    await websocket.send_text(
                        json.dumps({"type": "ping"}),
                    )
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    ping_task = asyncio.ensure_future(_ping_loop())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")
            if msg_type == "pong":
                continue
            elif msg_type == "mouse":
                await _handle_mouse(data, agent_id)
            elif msg_type == "keyboard":
                await _handle_keyboard(data, agent_id)
            elif msg_type in (
                "navigate",
                "navigate_back",
                "navigate_forward",
                "reload",
            ):
                await _handle_navigate(data, agent_id)
            elif msg_type == "switch_tab":
                await _handle_switch_tab(data, agent_id)
            elif msg_type == "new_tab":
                await _handle_new_tab(agent_id)
            elif msg_type == "close_tab":
                await _handle_close_tab(data, agent_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Browser WS error", exc_info=True)
    finally:
        ping_task.cancel()
        clients.discard(websocket)
        # Clean up empty sets
        if not clients:
            _ws_clients.pop(agent_id, None)
            asyncio.ensure_future(_stop_cdp_screencast(agent_id))
        _stop_screencaster()
