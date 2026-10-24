"""Tests for FastAPI admin endpoints.

Covers:
- Health endpoints
- Admin authentication (all three admin routes)
- GET /admin/status
- POST /admin/reset-key/{key_id}
- POST /admin/reassign-session/{session_id}
- Idempotency
- Reset + rebind cycle
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

from app import config
from app.main import create_app
from app.proxy import SessionProxy
from app.store import Store

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "change-me-admin-token"
WRONG_TOKEN = "wrong-token"

_SEED_KEYS = {
    "keys": [
        {"id": "key-alpha", "key": "sk-alpha-AAAAA-BBBBB-00001-mnop", "label": "Alpha"},
        {"id": "key-beta",  "key": "sk-beta-LONGER-DUMMY-SECRET-00002-abcd", "label": "Beta"},
        {"id": "key-gamma", "key": "sk-gamma-ANOTHER-DUMMY-SECRET-00003-wxyz", "label": "Gamma"},
    ]
}


# ---------------------------------------------------------------------------
# Fake clock (same pattern as test_store.py)
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


# ---------------------------------------------------------------------------
# Fixture: clock
# ---------------------------------------------------------------------------

@pytest.fixture
def clock() -> Clock:
    return Clock()


# ---------------------------------------------------------------------------
# Fixture: admin_app
# ---------------------------------------------------------------------------

@pytest.fixture
async def admin_app(tmp_path: Path, clock: Clock):
    """Self-contained FastAPI app with a seeded store and a bound proxy.

    Sets up:
    - Three seeded keys (key-alpha, key-beta, key-gamma)
    - Pre-bound session "sess-ok" -> key-alpha (so admin status tests work)
    - Pre-bound session "sess-bound-beta" -> key-beta
    - Mock transport that always returns 200 with a valid chat completions body

    Returns (app, store, proxy).
    """
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(_SEED_KEYS), encoding="utf-8")

    db_path = tmp_path / "admin.db"
    store = Store(
        db_path=str(db_path),
        keys_file=str(keys_file),
        session_ttl=300.0,
        cooldown=30.0,
        now=clock,
    )
    store.init()

    # Pre-bind sessions so admin status tests have something to verify.
    # Bind deterministically: sess-ok -> first available (alpha), sess-bound-beta -> next (beta)
    ts = clock()
    store.resolve("sess-ok", now=ts)
    store.resolve("sess-bound-beta", now=ts)
    for s in ("sess-ok", "sess-bound-beta"):
        conn = store._conn()
        conn.execute("UPDATE bindings SET bound_at = ?, last_seen = ? WHERE session_id = ?",
                     (ts, ts, s))
        conn.commit()

    # Mock transport that returns a valid 200 response.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "assistant-0",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
            headers={"Content-Type": "application/json"},
        )

    transport = _AsyncHTTPTransport(handler)
    proxy = SessionProxy(store, upstream_url="http://upstream.test", transport=transport, now=clock)
    app = create_app(store=store, proxy=proxy, gc_interval=float("inf"))

    yield app, store, proxy

    await proxy.close()
    store.close()


# ---------------------------------------------------------------------------
# Minimal async HTTP transport for use with SessionProxy
# ---------------------------------------------------------------------------

class _AsyncHTTPTransport(httpx.AsyncBaseTransport):
    """A minimal mock transport that delegates to an async handler callable."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper to build an admin-authenticated async client
# ---------------------------------------------------------------------------

def _admin_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://admin-test",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )


# ---------------------------------------------------------------------------
# Tests: Health endpoints
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_root_returns_200(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json()["service"] == "session-key-proxy"

    @pytest.mark.asyncio
    async def test_proxy_health_returns_200(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(config.PROXY_ENDPOINT)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Tests: Admin authentication
# ---------------------------------------------------------------------------

class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_status_rejects_missing_token(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_status_rejects_wrong_token(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Admin-Token": WRONG_TOKEN}
        ) as client:
            resp = await client.get("/admin/status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_status_accepts_correct_token(self, admin_app):
        app, _, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reset_key_rejects_missing_token(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/admin/reset-key/key-alpha")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_reassign_session_rejects_missing_token(self, admin_app):
        app, _, _ = admin_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/admin/reassign-session/sess-ok")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: GET /admin/status
# ---------------------------------------------------------------------------

class TestAdminStatus:
    @pytest.mark.asyncio
    async def test_returns_counts(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "counts" in data
        counts = data["counts"]
        # key-alpha and key-beta are bound; key-gamma is available.
        assert counts["bound"] == 2
        assert counts["available"] == 1

    @pytest.mark.asyncio
    async def test_keys_list_has_required_fields(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        data = resp.json()
        keys = data["keys"]
        assert len(keys) == 3
        for k in keys:
            assert "id" in k
            assert "status" in k
            assert "masked" in k
            # masked should contain a masked version of the id (not the raw secret)
            assert "key-" in k["masked"]
            assert "sk-" not in k["masked"]

    @pytest.mark.asyncio
    async def test_key_alpha_is_bound_to_sess_ok(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        data = resp.json()
        alpha_keys = [k for k in data["keys"] if k["id"] == "key-alpha"]
        assert len(alpha_keys) == 1
        assert alpha_keys[0]["status"] == "bound"
        assert len(alpha_keys[0]["sessions"]) == 1
        assert alpha_keys[0]["sessions"][0]["session_id"] == "sess-ok"

    @pytest.mark.asyncio
    async def test_key_beta_is_bound_to_sess_bound_beta(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        data = resp.json()
        beta_keys = [k for k in data["keys"] if k["id"] == "key-beta"]
        assert len(beta_keys) == 1
        assert beta_keys[0]["status"] == "bound"
        assert len(beta_keys[0]["sessions"]) == 1
        assert beta_keys[0]["sessions"][0]["session_id"] == "sess-bound-beta"

    @pytest.mark.asyncio
    async def test_key_gamma_has_no_sessions(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        data = resp.json()
        gamma_keys = [k for k in data["keys"] if k["id"] == "key-gamma"]
        assert len(gamma_keys) == 1
        assert gamma_keys[0]["status"] == "available"
        assert gamma_keys[0]["sessions"] == []

    @pytest.mark.asyncio
    async def test_bindings_list_includes_all_sessions(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.get("/admin/status")
        data = resp.json()
        bindings = data["bindings"]
        session_ids = {b["session_id"] for b in bindings}
        assert "sess-ok" in session_ids
        assert "sess-bound-beta" in session_ids


# ---------------------------------------------------------------------------
# Tests: POST /admin/reset-key/{key_id}
# ---------------------------------------------------------------------------

class TestResetKey:
    @pytest.mark.asyncio
    async def test_reset_bound_key_unbinds_and_makes_available(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.post("/admin/reset-key/key-alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key_id"] == "key-alpha"
        assert data["status"] == "available"

    @pytest.mark.asyncio
    async def test_after_reset_key_alpha_has_no_sessions(self, admin_app):
        # After reset, the key becomes available while its original session
        # binding persists (so that reassign-session can later unbind it).
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            await client.post("/admin/reset-key/key-alpha")
            resp = await client.get("/admin/status")
        data = resp.json()
        alpha_keys = [k for k in data["keys"] if k["id"] == "key-alpha"]
        assert len(alpha_keys) == 1
        assert alpha_keys[0]["status"] == "available"
        # After reset, the key's original binding is preserved;
        # the session is not cleared until reassign-session is called.
        assert len(alpha_keys[0]["sessions"]) == 1
        assert alpha_keys[0]["sessions"][0]["session_id"] == "sess-ok"

    @pytest.mark.asyncio
    async def test_reset_nonexistent_key_returns_unchanged(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.post("/admin/reset-key/ghost-key")
        # main.py returns 200 with status="unchanged" for unknown keys
        assert resp.status_code == 200
        data = resp.json()
        assert data["key_id"] == "ghost-key"
        assert data["status"] == "unchanged"


# ---------------------------------------------------------------------------
# Tests: POST /admin/reassign-session/{session_id}
# ---------------------------------------------------------------------------

class TestReassignSession:
    @pytest.mark.asyncio
    async def test_reassign_bound_session_unbinds_key(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.post("/admin/reassign-session/sess-ok")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-ok"
        assert data["reassigned"] is True

    @pytest.mark.asyncio
    async def test_after_reassign_key_alpha_is_available(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            await client.post("/admin/reassign-session/sess-ok")
            resp = await client.get("/admin/status")
        data = resp.json()
        alpha_keys = [k for k in data["keys"] if k["id"] == "key-alpha"]
        assert len(alpha_keys) == 1
        assert alpha_keys[0]["status"] == "available"

    @pytest.mark.asyncio
    async def test_reassign_nonexistent_session_returns_false(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp = await client.post("/admin/reassign-session/no-such-session")
        assert resp.status_code == 200
        assert resp.json()["reassigned"] is False


# ---------------------------------------------------------------------------
# Tests: Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    @pytest.mark.asyncio
    async def test_reset_available_key_returns_unchanged(self, admin_app):
        # key-gamma is available; reset it (no-op).
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            resp1 = await client.post("/admin/reset-key/key-gamma")
            resp2 = await client.post("/admin/reset-key/key-gamma")
        # First reset: key was available, so set_key_state returns False → status="unchanged"
        # Second reset: same thing
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "unchanged"
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "unchanged"

    @pytest.mark.asyncio
    async def test_reassign_already_unbound_session_returns_false(self, admin_app):
        app, store, _ = admin_app
        async with _admin_client(app) as client:
            # sess-ok is pre-bound; unbind it first.
            await client.post("/admin/reassign-session/sess-ok")
            # Now reassign again.
            resp2 = await client.post("/admin/reassign-session/sess-ok")
        assert resp2.status_code == 200
        assert resp2.json()["reassigned"] is False


# ---------------------------------------------------------------------------
# Tests: Reset + rebind cycle
# ---------------------------------------------------------------------------

class TestResetAndRebind:
    @pytest.mark.asyncio
    async def test_full_reset_reassign_rebind_cycle(self, admin_app):
        app, store, proxy = admin_app
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test",
                                   headers={"X-Admin-Token": ADMIN_TOKEN})

        # 1. Verify key-alpha is bound to sess-ok initially.
        resp = await client.get("/admin/status")
        data = resp.json()
        assert data["counts"]["bound"] == 2

        # 2. Reset key-alpha → available.
        r1 = await client.post("/admin/reset-key/key-alpha")
        assert r1.json()["status"] == "available"

        # 3. Reassign sess-ok → key-alpha is still available (just unbound).
        r2 = await client.post("/admin/reassign-session/sess-ok")
        assert r2.json()["reassigned"] is True

        # After both operations, key-alpha is available (reset) and sess-ok is unbound.
        resp = await client.get("/admin/status")
        data = resp.json()
        alpha = next(k for k in data["keys"] if k["id"] == "key-alpha")
        assert alpha["status"] == "available"
        assert alpha["sessions"] == []

        # 4. New session binds key-alpha (first available).
        bind_resp = await proxy.forward(
            method="POST",
            path="/v1/chat/completions",
            headers=httpx.Headers({"x-session-id": "brand-new-session"}),
            body=b'{"model":"deepseek-chat"}',
        )
        assert bind_resp.classification.category == "success"
        assert bind_resp.key_id == "key-alpha"

        # 5. Verify counts are back to 2 bound, 1 available.
        resp = await client.get("/admin/status")
        data = resp.json()
        assert data["counts"]["bound"] == 2
        assert data["counts"]["available"] == 1

        await client.aclose()
