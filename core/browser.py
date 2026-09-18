import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Dict, Optional

from patchright.async_api import Playwright, Browser, BrowserContext, Page

from core.config import settings
from core.session_store import (
    SessionConflictError,
    SessionStore,
    SessionStoreError,
    session_store,
)

logger = logging.getLogger(__name__)

MAX_PAGES_PER_BROWSER = settings.max_pages_per_browser


class BrowserInfo:
    """Container for a connected browser, its default context, and legacy pages."""

    def __init__(self, browser: Browser, context: BrowserContext, ws_url: str = "", browser_id: str = "", headers: Optional[dict] = None):
        self.browser = browser
        self.context = context
        self.ws_url = ws_url
        self.browser_id = browser_id
        # Optional auth headers sent on CDP connect (BYO/remote browsers).
        self.headers = headers or {}
        # Legacy named session pages share the default context. Durable sessions
        # live separately in BrowserManager.persistent_sessions.
        self.pages: Dict[str, Page] = {}


@dataclass
class PersistentSessionHandle:
    """Live dedicated context for one encrypted session registration."""

    session_id: str
    browser_id: str
    browser: Browser
    context: BrowserContext
    page: Page
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserManager:
    """
    Agnostic browser manager — connects to browsers purely via CDP WebSocket URLs.

    The manager does NOT know about launchers, profiles, or orchestration.
    External systems (operator, API calls) register browsers by providing a
    ``browser_id`` and a ``ws_url``.
    """

    def __init__(self, store: Optional[SessionStore] = None):
        self.playwright: Optional[Playwright] = None
        self.browsers: Dict[str, BrowserInfo] = {}
        self.session_store = store or session_store
        self.persistent_sessions: Dict[str, PersistentSessionHandle] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._reconnect_lock = asyncio.Lock()
        self._playwright_pid: Optional[int] = None

    async def start(self, playwright: Playwright):
        """Initialise with a Playwright instance. No auto-connections."""
        self.playwright = playwright
        self._track_playwright_pid()
        logger.info("Browser manager started (agnostic mode — waiting for registrations)")

    def _driver_proc(self):
        """The Node driver subprocess behind the pipe transport, or None.

        Reaches through private attributes (impl connection -> pipe transport),
        so every step is guarded — a layout change just disables the feature.
        """
        try:
            return self.playwright._impl_obj._connection._transport._proc
        except AttributeError:
            return None

    def _track_playwright_pid(self):
        proc = self._driver_proc()
        if proc is not None:
            self._playwright_pid = proc.pid
            logger.info(f"Tracking Playwright driver PID: {self._playwright_pid}")

    def driver_alive(self) -> bool:
        """Whether the Playwright Node driver process is still running.

        Returns True when the process can't be introspected (private layout
        changed) so a false negative can never crash-loop the pod.
        """
        if not self.playwright:
            return False
        proc = self._driver_proc()
        if proc is None:
            return True
        # returncode is None while running, an int once exited. Only a
        # definite int counts as dead (doubles/mocks stay "alive").
        return not isinstance(proc.returncode, int)

    def _kill_playwright_process(self):
        if self._playwright_pid:
            try:
                os.kill(self._playwright_pid, signal.SIGKILL)
                logger.warning(f"Force-killed old Playwright driver PID {self._playwright_pid}")
            except (ProcessLookupError, PermissionError):
                pass
            self._playwright_pid = None

    # ── durable dedicated sessions ───────────────────────────

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    @staticmethod
    def _proxy_settings(proxy: dict) -> dict:
        host = proxy["host"]
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        settings = {
            "server": "%s://%s:%s" % (proxy["type"], host, proxy["port"]),
        }
        if proxy.get("username") is not None:
            settings["username"] = proxy["username"]
        if proxy.get("password") is not None:
            settings["password"] = proxy["password"]
        if proxy.get("bypass") is not None:
            settings["bypass"] = proxy["bypass"]
        return settings

    def is_persistent_session(self, session_id: str) -> bool:
        return self.session_store.exists(session_id)

    def active_session_count(self, browser_id: str) -> int:
        legacy = len(self.browsers[browser_id].pages) if browser_id in self.browsers else 0
        dedicated = sum(
            1 for handle in self.persistent_sessions.values()
            if handle.browser_id == browser_id
        )
        return legacy + dedicated

    async def activate_persistent_session(
        self, browser_info: BrowserInfo, session_id: str
    ) -> PersistentSessionHandle:
        """Lazily restore one dedicated context from encrypted state."""
        async with self._session_lock(session_id):
            record = self.session_store.get(session_id)
            registered_browser = record.get("browser_id")
            if registered_browser and registered_browser != browser_info.browser_id:
                raise SessionConflictError("Session is registered to another browser")
            if not registered_browser:
                record = self.session_store.bind_browser(
                    session_id, browser_info.browser_id
                )

            handle = self.persistent_sessions.get(session_id)
            if handle is not None:
                usable = handle.browser is browser_info.browser
                try:
                    usable = usable and handle.browser.is_connected() and not handle.page.is_closed()
                    if usable:
                        _ = handle.page.url
                except Exception:
                    usable = False
                if usable:
                    return handle
                self.persistent_sessions.pop(session_id, None)
                try:
                    await handle.context.close()
                except Exception:
                    pass

            kwargs = {"proxy": self._proxy_settings(record["proxy"])}
            if record.get("storage_state") is not None:
                kwargs["storage_state"] = record["storage_state"]
            context = await browser_info.browser.new_context(**kwargs)
            try:
                page = await context.new_page()
            except Exception:
                await context.close()
                raise
            handle = PersistentSessionHandle(
                session_id=session_id,
                browser_id=browser_info.browser_id,
                browser=browser_info.browser,
                context=context,
                page=page,
            )
            self.persistent_sessions[session_id] = handle
            logger.info(
                "Activated dedicated persistent session %s in browser '%s'",
                session_id,
                browser_info.browser_id,
            )
            return handle

    async def snapshot_persistent_session(
        self, session_id: str, handle: Optional[PersistentSessionHandle] = None
    ) -> bool:
        current = handle or self.persistent_sessions.get(session_id)
        if current is None or self.persistent_sessions.get(session_id) is not current:
            return False
        state = await current.context.storage_state(indexed_db=True)
        self.session_store.save_storage_state(session_id, state)
        return True

    async def close_persistent_session(
        self, session_id: str, snapshot: bool = True
    ) -> bool:
        """Snapshot and close an active context while retaining registration."""
        async with self._session_lock(session_id):
            handle = self.persistent_sessions.get(session_id)
            if handle is None:
                return False
            async with handle.request_lock:
                if snapshot:
                    # A failed durable write must leave the live context intact so
                    # the caller can retry instead of silently losing login state.
                    await self.snapshot_persistent_session(session_id, handle)
                self.persistent_sessions.pop(session_id, None)
                try:
                    await handle.context.close()
                except Exception as exc:
                    logger.warning(
                        "Could not close persistent session %s (%s)",
                        session_id,
                        type(exc).__name__,
                    )
            return True

    async def close_persistent_sessions_for_browser(
        self, browser_id: str, snapshot: bool = True
    ) -> None:
        session_ids = [
            session_id
            for session_id, handle in list(self.persistent_sessions.items())
            if handle.browser_id == browser_id
        ]
        for session_id in session_ids:
            await self.close_persistent_session(session_id, snapshot=snapshot)

    async def register_persistent_session(
        self,
        session_id: str,
        proxy: dict,
        browser_id: Optional[str] = None,
        replace: bool = False,
    ) -> str:
        """Register configuration under the same lock used for activation."""
        async with self._session_lock(session_id):
            different = self.session_store.exists(session_id) and not self.session_store.registration_matches(
                session_id, proxy, browser_id
            )
            if different and replace:
                handle = self.persistent_sessions.get(session_id)
                if handle is not None:
                    async with handle.request_lock:
                        try:
                            await handle.context.close()
                        except Exception as exc:
                            raise SessionStoreError(
                                "Could not close the active session before replacement"
                            ) from exc
                        self.persistent_sessions.pop(session_id, None)
            return self.session_store.register(
                session_id=session_id,
                proxy=proxy,
                browser_id=browser_id,
                replace=replace,
            )

    async def delete_persistent_session(self, session_id: str) -> bool:
        """Close and atomically remove a registration plus saved browser state."""
        lock = self._session_lock(session_id)
        async with lock:
            handle = self.persistent_sessions.get(session_id)
            if handle is not None:
                async with handle.request_lock:
                    try:
                        await handle.context.close()
                    except Exception as exc:
                        raise SessionStoreError(
                            "Could not close the active session before deletion"
                        ) from exc
                    self.persistent_sessions.pop(session_id, None)
            deleted = self.session_store.delete(session_id)
        return deleted

    # ── connect / disconnect ─────────────────────────────────

    async def connect_browser(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Connect to a remote browser over CDP.

        If ``browser_id`` is already connected **with the same URL** and the
        connection is still alive, the existing connection is returned.
        If the connection is dead (e.g. browser restarted), it auto-reconnects.
        If the URL differs, the old connection is dropped and a new one opened.

        ``headers`` are optional HTTP headers sent on the CDP connect, used for
        BYO/remote browsers that require auth (e.g. an Authorization bearer).
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if browser_id in self.browsers:
            existing = self.browsers[browser_id]
            if existing.ws_url == ws_url and existing.browser.is_connected():
                logger.info(f"Browser '{browser_id}' already connected (idempotent)")
                return existing
            if existing.ws_url != ws_url:
                logger.info(
                    f"Browser '{browser_id}' ws_url changed "
                    f"({existing.ws_url} -> {ws_url}), reconnecting"
                )
            else:
                logger.info(f"Browser '{browser_id}' connection is dead, reconnecting...")
            await self.disconnect_browser(browser_id)

        logger.info(f"Connecting to browser '{browser_id}' via {ws_url}")
        browser = await self.playwright.chromium.connect_over_cdp(ws_url, headers=headers or None)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {})
        self.browsers[browser_id] = info
        logger.info(f"Connected to browser '{browser_id}'")
        return info

    async def disconnect_browser(self, browser_id: str, snapshot: bool = True) -> bool:
        """Disconnect a browser and close all its session pages."""
        if browser_id not in self.browsers:
            return False

        info = self.browsers[browser_id]

        await self.close_persistent_sessions_for_browser(browser_id, snapshot=snapshot)
        self.browsers.pop(browser_id, None)

        for page in list(info.pages.values()):
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page in '{browser_id}': {e}")

        try:
            await info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser '{browser_id}': {e}")

        logger.info(f"Disconnected browser '{browser_id}'")
        return True

    # ── lookup helpers ───────────────────────────────────────

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a connected browser by ID. Raises ``KeyError`` if not found."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser '{browser_id}' not connected")
        return self.browsers[browser_id]

    def first_browser_id(self) -> Optional[str]:
        return next(iter(self.browsers), None)

    def least_loaded_browser_id(self) -> Optional[str]:
        best_id: Optional[str] = None
        best_count = float("inf")
        for bid in self.browsers:
            count = self.active_session_count(bid)
            if count < best_count and count < MAX_PAGES_PER_BROWSER:
                best_count = count
                best_id = bid
        return best_id

    # ── recovery ────────────────────────────────────────────

    async def recover_connection(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Recover a broken browser connection.

        Uses a lock so that concurrent requests don't all try to recover at
        the same time.  Two recovery levels are attempted:

        * Level 1 – disconnect the stale entry and reconnect via the
          **existing** Playwright driver.
        * Level 2 – if the driver pipe is broken, restart the entire
          Playwright driver process and reconnect every browser.

        ``headers`` defaults to the existing connection's headers (so BYO auth
        survives a reconnect) when not provided by the caller.
        """
        async with self._reconnect_lock:
            # Another request may have already recovered while we waited
            if browser_id in self.browsers:
                existing = self.browsers[browser_id]
                if headers is None:
                    headers = existing.headers
                # Only short-circuit if the URL also matches — ws_url drift
                # (browser pod restart with new IP/port) means we MUST reconnect.
                if existing.ws_url == ws_url and existing.browser.is_connected():
                    return existing

            # ── Level 1: simple reconnect with same Playwright driver ──
            try:
                return await self._reconnect_same_driver(browser_id, ws_url, headers)
            except Exception as e:
                logger.warning(
                    f"Level-1 reconnect failed for '{browser_id}': {e}"
                )

            # ── Level 2: restart Playwright driver entirely ──
            return await self._restart_playwright_and_reconnect(browser_id, ws_url, headers)

    async def _reconnect_same_driver(
        self, browser_id: str, ws_url: str, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Disconnect stale entry and open a fresh CDP connection."""
        try:
            await asyncio.wait_for(
                self.disconnect_browser(browser_id, snapshot=False), timeout=10.0
            )
        except Exception as e:
            logger.warning(f"Error disconnecting '{browser_id}': {e}")
            self.browsers.pop(browser_id, None)

        browser = await self.playwright.chromium.connect_over_cdp(ws_url, headers=headers or None)
        context = (
            browser.contexts[0] if browser.contexts
            else await browser.new_context()
        )
        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {})
        self.browsers[browser_id] = info
        logger.info(f"Reconnected browser '{browser_id}' (same driver)")
        return info

    async def _restart_playwright_and_reconnect(
        self, browser_id: str, fresh_ws_url: Optional[str] = None, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Restart the Playwright driver process and reconnect every browser."""
        logger.warning("Restarting Playwright driver for full recovery")

        saved = {bid: (info.ws_url, info.headers) for bid, info in self.browsers.items()}
        if fresh_ws_url:
            saved[browser_id] = (fresh_ws_url, headers if headers is not None else saved.get(browser_id, (None, {}))[1])
        for session_id in list(self.persistent_sessions):
            await self.close_persistent_session(session_id, snapshot=False)
        self.browsers.clear()

        old_pw = self.playwright
        self.playwright = None

        if old_pw is not None:
            try:
                await asyncio.wait_for(old_pw.stop(), timeout=5.0)
            except Exception:
                logger.warning(
                    "Old Playwright driver did not stop cleanly, force-killing"
                )
                self._kill_playwright_process()

        # Brief pause to let the OS reclaim sockets / pipes
        await asyncio.sleep(0.5)

        from patchright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self._track_playwright_pid()
        logger.info("Playwright driver restarted")

        new_info: Optional[BrowserInfo] = None
        for bid, (url, hdrs) in saved.items():
            try:
                browser = await asyncio.wait_for(
                    self.playwright.chromium.connect_over_cdp(url, headers=hdrs or None),
                    timeout=15.0,
                )
                context = (
                    browser.contexts[0] if browser.contexts
                    else await browser.new_context()
                )
                info = BrowserInfo(browser, context, ws_url=url, browser_id=bid, headers=hdrs or {})
                self.browsers[bid] = info
                if bid == browser_id:
                    new_info = info
                logger.info(f"Reconnected '{bid}' after Playwright restart")
            except Exception as e:
                logger.error(
                    f"Failed to reconnect '{bid}' after Playwright restart: {e}"
                )

        if new_info is None:
            raise RuntimeError(
                f"Failed to recover browser '{browser_id}' "
                "after Playwright restart"
            )
        return new_info

    # ── lifecycle ────────────────────────────────────────────

    async def cleanup_stale_pages(self) -> int:
        """Close session pages that are no longer usable.

        Patchright's Node driver leaks memory as dead/zombie pages accumulate
        (each holds driver-side references), eventually OOM-crashing it. We drop
        pages whose handle is closed or whose context is gone. Returns the count.
        """
        closed = 0
        for info in list(self.browsers.values()):
            for sid, page in list(info.pages.items()):
                dead = False
                try:
                    if page.is_closed():
                        dead = True
                    else:
                        _ = page.url  # touch — raises if the page/context is gone
                except Exception:
                    dead = True
                if dead:
                    info.pages.pop(sid, None)
                    try:
                        await page.close()
                    except Exception:
                        pass
                    closed += 1
        for session_id, handle in list(self.persistent_sessions.items()):
            dead = False
            try:
                if handle.page.is_closed() or not handle.browser.is_connected():
                    dead = True
                else:
                    _ = handle.page.url
            except Exception:
                dead = True
            if dead:
                # Dead contexts cannot be snapshotted; the most recent successful
                # per-request snapshot remains the recovery point.
                await self.close_persistent_session(session_id, snapshot=False)
                closed += 1
        if closed:
            logger.info(f"Stale page cleanup: closed {closed} dead page(s)")
        return closed

    async def shutdown(self, timeout: float = 25.0):
        """Disconnect all browsers."""
        logger.info("Starting browser manager shutdown…")

        async def _shutdown():
            for session_id in list(self.persistent_sessions):
                await self.close_persistent_session(session_id, snapshot=True)
            for bid in list(self.browsers.keys()):
                info = self.browsers[bid]
                for page in list(info.pages.values()):
                    try:
                        await page.close()
                    except Exception:
                        pass
                try:
                    await info.browser.close()
                except Exception:
                    pass
            self.browsers.clear()
            logger.info("All browsers disconnected")

        try:
            await asyncio.wait_for(_shutdown(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s, forcing cleanup")
            self.persistent_sessions.clear()
            self.browsers.clear()


# Global singleton
browser_manager = BrowserManager()
