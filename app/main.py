"""FastAPI application for the Session-Bound API Key Proxy."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.proxy import NoKeyAvailable, NoSessionHeader, ProxyResponse, SessionProxy
from app.store import (K_AVAILABLE, K_BOUND, K_COOLDOWN, K_EXHAUSTED, K_INVALID,
                       Binding, Store)
from app.util import mask_key

_ALL_KEY_STATES = (K_AVAILABLE, K_BOUND, K_COOLDOWN, K_EXHAUSTED, K_INVALID)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admin auth dependency
# ---------------------------------------------------------------------------

async def _require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> str:
    """Validate the X-Admin-Token header against the configured admin token."""
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing admin token",
        )
    return x_admin_token


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def _build_response(resp: ProxyResponse) -> Response:
    """Convert a ProxyResponse into a FastAPI Response.

    Filters out hop-by-hop headers and handles both streaming (2xx) and
    buffered (non-2xx / error) responses.
    """
    hop_by_hop = frozenset({
        "transfer-encoding",
        "connection",
        "keep-alive",
        "content-length",
        "trailer",
        "upgrade",
    })
    headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in hop_by_hop
    }

    if resp.upstream is not None:
        async def stream():
            try:
                async for chunk in resp.upstream.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=resp.status_code,
            headers=headers,
            media_type=headers.get("Content-Type"),
        )

    return Response(
        content=resp.content or b"",
        status_code=resp.status_code,
        headers=headers,
        media_type=headers.get("Content-Type"),
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    store: Optional[Store] = None,
    proxy: Optional[SessionProxy] = None,
    gc_interval: float = 60.0,
) -> FastAPI:
    """Build the FastAPI application, optionally with injected dependencies."""

    _store: Store = store if store is not None else Store(
        db_path=config.DB_PATH,
        keys_file=str(config.KEYS_FILE),
        session_ttl=config.SESSION_TTL_SECONDS,
        cooldown=config.COOLDOWN_SECONDS,
    )
    _store.init()

    _proxy: SessionProxy = proxy if proxy is not None else SessionProxy(_store)

    async def _gc_loop() -> None:
        while True:
            await asyncio.sleep(gc_interval)
            try:
                _store.gc()
            except Exception as exc:
                logger.warning("gc failed: %s", exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gc_task = asyncio.create_task(_gc_loop())
        logger.info("GC loop started")
        try:
            yield
        finally:
            gc_task.cancel()
            with suppress(asyncio.CancelledError):
                await gc_task
            await _proxy.close()
            _store.close()
            logger.info("Shutdown complete")

    app = FastAPI(
        title="Session-Bound API Key Proxy",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Serve static dashboard
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        """Serve the admin dashboard."""
        return FileResponse("app/static/index.html")

    PROXY_PATH = config.PROXY_ENDPOINT

    # ------------------------------------------------------------------
    # Root / health
    # ------------------------------------------------------------------

    @app.get("/")
    async def root() -> Dict[str, str]:
        """Health check for the root endpoint."""
        return {"service": "session-key-proxy", "version": "1.0.0"}

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        """Health check for Render healthcheck."""
        return {"status": "ok"}

    @app.get(PROXY_PATH)
    async def proxy_health() -> Dict[str, str]:
        """Health check for the proxy endpoint (supports GET for load-balancer probes)."""
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Proxy endpoint
    # ------------------------------------------------------------------

    @app.api_route(
        PROXY_PATH,
        methods=["POST", "GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy_route(request: Request) -> Response:
        """Forward a request to the upstream API.

        The session is identified by the x-session-id header. On first use a key
        is picked from the pool and bound to the session.
        """
        body: Optional[bytes] = await request.body()

        try:
            resp = await _proxy.forward(
                method=request.method,
                path=request.url.path,
                headers=dict(request.headers),
                body=body,
            )
        except NoSessionHeader:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing session id header",
            )
        except NoKeyAvailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no API key available",
            )

        return _build_response(resp)

    # ------------------------------------------------------------------
    # Admin endpoints
    # ------------------------------------------------------------------

    @app.get(config.STATUS_PATH)
    async def admin_status(_token: str = Depends(_require_admin)) -> Dict[str, Any]:
        """Return current proxy state: keys (with masked ids), bindings, and per-status counts."""
        keys = _store.list_keys()
        bindings = _store.list_bindings()

        key_sessions: Dict[str, List[Dict[str, Any]]] = {}
        for b in bindings:
            key_sessions.setdefault(b.key_id, []).append({
                "session_id": b.session_id,
                "bound_at": b.bound_at,
                "last_seen": b.last_seen,
            })

        key_list = []
        for k in keys:
            key_id = k["id"]
            masked = f"{key_id[:4]}{'*' * max(1, len(key_id) - 4)}"
            key_list.append({
                "id": key_id,
                "masked": masked,
                "label": k.get("label", ""),
                "status": k["status"],
                "cooldown_until": k.get("cooldown_until"),
                "updated_at": k["updated_at"],
                "sessions": key_sessions.get(key_id, []),
            })

        return {
            "counts": _store.count_by_status(),
            "keys": key_list,
            "bindings": [
                {
                    "session_id": b.session_id,
                    "key_id": b.key_id,
                    "bound_at": b.bound_at,
                    "last_seen": b.last_seen,
                }
                for b in bindings
            ],
        }

    @app.post(config.RESET_KEY_PATH)
    async def reset_key(
        key_id: str,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Reset a key back to the AVAILABLE state regardless of its current state."""
        # Idempotent: if key not found or already available, report unchanged.
        keys_by_id = {k["id"]: k for k in _store.list_keys()}
        existing = keys_by_id.get(key_id)
        if existing is None or existing["status"] == K_AVAILABLE:
            return JSONResponse({"key_id": key_id, "status": "unchanged"})
        # Key exists and is not available: flip it to available (binding remains).
        ok = _store.set_key_state(key_id, K_AVAILABLE)
        return JSONResponse({"key_id": key_id, "status": "available" if ok else "unchanged"})

    @app.post(config.REASSIGN_PATH)
    async def reassign_session(
        session_id: str,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Unbind a session so its next request picks a fresh key from the pool."""
        ok = _store.unbind(session_id)
        return JSONResponse({"session_id": session_id, "reassigned": ok})

    # JSON-body variants used by the dashboard (kept separate from the
    # path-param variants above for backward compatibility with the CLI).

    @app.post("/admin/set-key-state")
    async def set_key_state_json(
        payload: dict,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Set a key's state from the dashboard.

        Expected payload: {"key_id": str, "state": str}.
        state: available | bound | cooldown | exhausted | invalid
        """
        key_id = str(payload.get("key_id", "")).strip()
        state = str(payload.get("state", "")).strip()
        if not key_id or state not in _ALL_KEY_STATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="key_id (and a valid state) are required",
            )
        ok = _store.set_key_state(key_id, state)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key not found",
            )
        return JSONResponse({"key_id": key_id, "state": state, "set": True})

    @app.post("/admin/reset-key")
    async def reset_key_json(
        payload: dict,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Reset a key to available from the dashboard.

        Expected payload: {"key_id": str}. The optional target_state is honored
        if present, otherwise the key is set to available.
        """
        key_id = str(payload.get("key_id", "")).strip()
        if not key_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="key_id is required",
            )
        keys_by_id = {k["id"]: k for k in _store.list_keys()}
        existing = keys_by_id.get(key_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key not found",
            )
        target = str(payload.get("target_state", "available")).strip()
        if target not in _ALL_KEY_STATES:
            target = K_AVAILABLE
        if existing["status"] == target:
            return JSONResponse({"key_id": key_id, "status": "unchanged"})
        ok = _store.set_key_state(key_id, target)
        return JSONResponse({"key_id": key_id, "status": target if ok else "unchanged"})

    # ------------------------------------------------------------------
    # Admin: key management (add / update / rename / delete) + unbind
    # ------------------------------------------------------------------

    @app.post(config.ADD_KEY_PATH)
    async def add_key(
        payload: dict,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Add a new key to the pool.

        Expected payload: {"key_id": str, "secret": str, "label": str (optional)}
        """
        key_id = str(payload.get("key_id", "")).strip()
        secret = str(payload.get("secret", "")).strip()
        if not key_id or not secret:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="key_id and secret are required",
            )
        inserted = _store.add_key(key_id=key_id, secret=secret, label=str(payload.get("label", "")))
        if not inserted:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="key id already exists",
            )
        return JSONResponse({"key_id": key_id, "added": True})

    @app.put(config.UPDATE_KEY_PATH)
    async def update_key(
        key_id: str,
        payload: dict,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Update a key's secret and/or label.

        Expected payload: {"secret": str (optional), "label": str (optional)}.
        Omitted fields are left unchanged.
        """
        secret = payload.get("secret")
        label = payload.get("label")
        if secret is not None:
            secret = str(secret).strip()
            if not secret:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="secret cannot be empty",
                )
        if label is not None:
            label = str(label).strip()
        updated = _store.update_key(
            key_id=key_id,
            secret=str(secret) if secret is not None else None,
            label=str(label) if label is not None else None,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key not found",
            )
        return JSONResponse({"key_id": key_id, "updated": True})

    @app.post(config.RENAME_KEY_PATH)
    async def rename_key(
        key_id: str,
        payload: dict,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Rename a key's label only.

        Expected payload: {"label": str}. Convenience wrapper around update.
        """
        label = str(payload.get("label", "")).strip()
        updated = _store.update_key(key_id=key_id, label=label)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key not found",
            )
        return JSONResponse({"key_id": key_id, "label": label, "renamed": True})

    @app.delete(config.DELETE_KEY_PATH)
    async def delete_key(
        key_id: str,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Delete a key from the pool (also removes its bindings)."""
        deleted = _store.delete_key(key_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="key not found",
            )
        return JSONResponse({"key_id": key_id, "deleted": True})

    @app.post(config.UNBIND_SESSION_PATH)
    async def unbind_session(
        session_id: str,
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Force-unbind a session so its next request picks a fresh key."""
        ok = _store.unbind(session_id)
        return JSONResponse({"session_id": session_id, "unbound": ok})

    @app.post("/admin/reload-keys")
    async def reload_keys(
        _token: str = Depends(_require_admin),
    ) -> JSONResponse:
        """Re-read keys.json and add any keys that are not already in the pool.

        Existing keys are left untouched (no secret overwrites). Useful after a
        redeploy when keys.json changed.
        """
        added: List[str] = []
        import json as _json
        try:
            raw = config.KEYS_FILE.read_text(encoding="utf-8")
            entries = _json.loads(raw).get("keys", [])
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"failed to read keys file: {exc}",
            )
        existing = {k["id"] for k in _store.list_keys()}
        for entry in entries:
            key_id = str(entry.get("id", "")).strip()
            secret = str(entry.get("key", "")).strip()
            if not key_id or not secret or key_id in existing:
                continue
            insert_ok = _store.add_key(
                key_id=key_id,
                secret=secret,
                label=str(entry.get("label", "")),
            )
            if insert_ok:
                added.append(key_id)
        return JSONResponse({"added": added, "total": len(existing) + len(added)})

    return app


# ---------------------------------------------------------------------------
# Default app instance — allows `uvicorn app.main:app` without arguments.
# ---------------------------------------------------------------------------

app = create_app()
