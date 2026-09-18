import logging
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Request, Response, status as http_status

from core.browser import browser_manager
from core.dependencies import BrowserIdDep, SessionIdDep, get_browser_info
from core.registry import browser_registry
from core.session_store import SessionConflictError, SessionStoreError
from models.requests import (
    ConnectBrowserRequest,
    RegisterSessionsRequest,
    StartSessionRequest,
    validate_session_id_value,
)
from models.responses import BrowserResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browsers & Sessions"])


def _session_metadata(record: dict) -> dict:
    proxy = record["proxy"]
    return {
        "session_id": record["session_id"],
        "browser_id": record.get("browser_id"),
        "proxy": {
            "type": proxy["type"],
            "host": proxy["host"],
            "port": proxy["port"],
            "bypass": proxy.get("bypass"),
            "has_credentials": bool(
                proxy.get("username") is not None or proxy.get("password") is not None
            ),
        },
        "active": record["session_id"] in browser_manager.persistent_sessions,
        "has_storage_state": record.get("storage_state") is not None,
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


@router.get("/browsers")
async def list_browsers() -> List[BrowserResponse]:
    """List all connected browsers and their active (not registered) sessions."""
    return [
        BrowserResponse(
            browser_id=bid,
            ws_url=info.ws_url,
            session_count=browser_manager.active_session_count(bid),
        )
        for bid, info in browser_manager.browsers.items()
    ]


@router.post("/browsers")
async def connect_browser(request: ConnectBrowserRequest) -> BrowserResponse:
    """Connect idempotently to a remote browser via its CDP WebSocket URL."""
    try:
        info = await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except ValueError:
        await browser_manager.disconnect_browser(request.browser_id)
        info = await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to connect: {exc}") from exc

    return BrowserResponse(
        browser_id=request.browser_id,
        ws_url=info.ws_url,
        session_count=browser_manager.active_session_count(request.browser_id),
    )


@router.delete("/browsers/{browser_id:path}")
async def disconnect_browser(browser_id: str) -> dict:
    """Disconnect a browser and close all its active sessions."""
    success = await browser_manager.disconnect_browser(browser_id)
    if success:
        return {"status": "success", "message": f"Browser '{browser_id}' disconnected"}
    raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not connected")


@router.post("/sessions/register")
async def register_sessions(body: RegisterSessionsRequest, response: Response) -> dict:
    """Register 1-100 definitions and report each item independently."""
    store = browser_manager.session_store
    results = []
    failed = 0
    for item in body.sessions:
        try:
            registration_status = await browser_manager.register_persistent_session(
                session_id=item.session_id,
                proxy=item.proxy.to_record(),
                browser_id=item.browser_id,
                replace=body.replace,
            )
            record = store.get(item.session_id)
        except SessionConflictError as exc:
            failed += 1
            results.append({
                "session_id": item.session_id,
                "registration_status": "conflict",
                "error": str(exc),
            })
            continue
        except SessionStoreError:
            failed += 1
            results.append({
                "session_id": item.session_id,
                "registration_status": "error",
                "error": "Persistent session registration failed",
            })
            continue
        metadata = _session_metadata(record)
        metadata["registration_status"] = registration_status
        results.append(metadata)
    if failed:
        response.status_code = http_status.HTTP_207_MULTI_STATUS
    return {"sessions": results, "partial": bool(failed)}


@router.get("/sessions")
async def list_sessions() -> dict:
    """List only redacted registration metadata and live activity state."""
    try:
        records = browser_manager.session_store.list()
    except SessionStoreError as exc:
        raise HTTPException(
            status_code=500, detail="Persistent session state could not be read"
        ) from exc
    return {"sessions": [_session_metadata(record) for record in records]}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Close and permanently purge a persistent session registration and state."""
    try:
        validate_session_id_value(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not browser_manager.session_store.exists(session_id):
        raise HTTPException(status_code=404, detail="Session is not registered")
    try:
        await browser_manager.delete_persistent_session(session_id)
    except SessionStoreError as exc:
        raise HTTPException(
            status_code=500, detail="Persistent session could not be deleted"
        ) from exc
    return {"status": "success", "message": f"Session {session_id} deleted"}


@router.post("/start_session")
async def start_session(
    request: Request,
    body: StartSessionRequest = StartSessionRequest(),
    browser_id: BrowserIdDep = None,
) -> dict:
    """Start a legacy shared page or activate a durable dedicated context."""
    store = browser_manager.session_store
    existing = None
    if body.session_id and store.exists(body.session_id):
        try:
            existing = store.get(body.session_id)
        except SessionStoreError as exc:
            raise HTTPException(
                status_code=500, detail="Persistent session state could not be read"
            ) from exc

    if body.session_id and existing is None and body.proxy is None:
        raise HTTPException(
            status_code=404,
            detail="Stable session is not registered; provide proxy settings or pre-register it",
        )

    requested_browser = browser_id or body.browser_id
    if existing and existing.get("browser_id"):
        if requested_browser and requested_browser != existing["browser_id"]:
            raise HTTPException(
                status_code=409, detail="Session is registered to another browser"
            )
        requested_browser = existing["browser_id"]

    browser_info = await get_browser_info(
        request=request, browser_id=requested_browser
    )

    if body.session_id:
        if existing is not None and body.proxy is not None:
            if existing["proxy"] != body.proxy.to_record():
                raise HTTPException(
                    status_code=409,
                    detail="Session is already registered with different proxy configuration",
                )
        if existing is None:
            try:
                await browser_manager.register_persistent_session(
                    session_id=body.session_id,
                    proxy=body.proxy.to_record(),
                    browser_id=browser_info.browser_id,
                )
            except SessionStoreError as exc:
                raise HTTPException(
                    status_code=500, detail="Persistent session registration failed"
                ) from exc

        try:
            await browser_manager.activate_persistent_session(
                browser_info, body.session_id
            )
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionStoreError as exc:
            raise HTTPException(
                status_code=500,
                detail="Persistent session state could not be read",
            ) from exc
        except Exception as exc:
            logger.warning(
                "start_session: persistent activation failed for %s; attempting recovery (%s)",
                body.session_id,
                type(exc).__name__,
            )
            fresh_ws_url = browser_registry.get_browser_ws_url(browser_info.browser_id)
            reconnect_url = fresh_ws_url or browser_info.ws_url
            try:
                browser_info = await browser_manager.recover_connection(
                    browser_info.browser_id,
                    reconnect_url,
                    headers=browser_registry.get_browser_headers(browser_info.browser_id),
                )
                await browser_manager.activate_persistent_session(
                    browser_info, body.session_id
                )
            except SessionStoreError as store_error:
                raise HTTPException(
                    status_code=500,
                    detail="Persistent session state could not be read",
                ) from store_error
            except Exception as recover_exc:
                raise HTTPException(
                    status_code=502,
                    detail="Failed to start persistent session after browser recovery",
                ) from recover_exc

        return {
            "session_id": body.session_id,
            "browser_id": browser_info.browser_id,
            "persistent": True,
            "message": "Persistent session activated. Use X-Session-Id and X-Browser-Id headers in subsequent requests.",
        }

    # Backward-compatible behavior: random ID and shared default browser context.
    try:
        page = await browser_info.context.new_page()
    except Exception as exc:
        logger.warning(f"start_session: failed to open page, attempting recovery: {exc}")
        fresh_ws_url = browser_registry.get_browser_ws_url(browser_info.browser_id)
        reconnect_url = fresh_ws_url or browser_info.ws_url
        try:
            browser_info = await browser_manager.recover_connection(
                browser_info.browser_id,
                reconnect_url,
                headers=browser_registry.get_browser_headers(browser_info.browser_id),
            )
            page = await browser_info.context.new_page()
        except Exception as recover_exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to start session after recovery: {recover_exc}",
            ) from recover_exc

    session_id = str(uuid.uuid4())
    browser_info.pages[session_id] = page
    logger.info(f"Started new session: {session_id} in browser '{browser_info.browser_id}'")

    return {
        "session_id": session_id,
        "browser_id": browser_info.browser_id,
        "message": "Session created. Use X-Session-Id and X-Browser-Id headers in subsequent requests.",
    }


@router.delete("/end_session")
async def end_session(
    request: Request,
    session_id: SessionIdDep = None,
    browser_id: BrowserIdDep = None,
) -> dict:
    """Close an active session; persistent registrations are retained for reuse."""
    if session_id is None:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required")

    manager = request.app.state.browser_manager
    if manager.is_persistent_session(session_id):
        try:
            active = await manager.close_persistent_session(session_id, snapshot=True)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="Persistent session could not be saved; it remains active",
            ) from exc
        message = (
            f"Persistent session {session_id} ended and retained"
            if active
            else f"Persistent session {session_id} is already inactive and retained"
        )
        return {"status": "success", "message": message}

    browser_info = await get_browser_info(request=request, browser_id=browser_id)
    page = browser_info.pages.pop(session_id, None)
    if page:
        try:
            await page.close()
            logger.info(f"Closed page for session {session_id}")
            return {"status": "success", "message": f"Session {session_id} ended"}
        except Exception as exc:
            logger.warning(f"Error closing page for session {session_id}: {exc}")
            return {
                "status": "success",
                "message": f"Session {session_id} removed (page was already closed)",
            }
    return {
        "status": "success",
        "message": f"Session {session_id} not found (already ended or never existed)",
    }
