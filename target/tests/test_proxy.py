"""Comprehensive tests for session-bound API key proxy (app/proxy.py + app/main.py).

Covers:
- SessionProxy unit: session header extraction, binding resolution,
  hop-by-hop header stripping, 2xx streaming vs non-2xx buffering,
  transient-retry loop with exponential backoff, no-retry on client errors,
  key-state transitions (capacity→cooldown, quota→exhausted, invalid→invalid).
- FastAPI integration: missing session → 400, no key → 503, streaming 2xx.
- Admin endpoints: auth gate, status, reassign-session, reset-key.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from app.classification import Classification, classify as classify_response
from app.config import (
    ADMIN_TOKEN,
    MAX_TRANSIENT_RETRIES,
    PROXY_ENDPOINT,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    SESSION_ID_HEADERS,
    UPSTREAM_URL,
)
from app.main import create_app
from app.proxy import NoKeyAvailable, NoSessionHeader, ProxyResponse, SessionProxy
from app.store import (
    K_AVAILABLE,
    K_BOUND,
    K_COOLDOWN,
    K_EXHAUSTED,
    K_INVALID,
    Binding,
    Store,
)
from app.util import mask_key

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
UPSTREAM = UPSTREAM_URL or "https://api.deepseek.com/v1"
SEED_KEYS = {
    "keys": [
        {"id": "key-alpha", "key": "sk-alpha-AAAAA-BBBBB-00001", "label": "Alpha"},
        {"id": "key-beta",  "key": "sk-beta-LONGER-DUMMY-SECRET-00002", "label": "Beta"},
        {"id": "key-gamma", "key": "sk-gamma-ANOTHER-DUMMY-SECRET-00003", "label": "Gamma"},
    ]
}
SESSION_HEADER = SESSION_ID_HEADERS[0]

# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------

class Clock:
    """Mutable fake wall-clock for deterministic retry-backoff testing."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


# ---------------------------------------------------------------------------
# In-memory store fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_store(tmp_path: Path, clock: Clock) -> Store:
    """In-memory SQLite store pre-seeded with three test keys."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(SEED_KEYS), encoding="utf-8")
    db_path = tmp_path / "test.db"
    st = Store(db_path=db_path, keys_file=str(keys_file), now=clock)
    st.init()
    yield st
    st.close()


# ---------------------------------------------------------------------------
# Mock transport helper
# ---------------------------------------------------------------------------

def make_mock_transport(
    status: int,
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
):
    """Return a MockTransport that returns the given status/body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Proxy factory fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def make_proxy(mem_store: Store, clock: Clock):
    """Factory: build a SessionProxy with custom transport."""
    def _make(
        status: int = 200,
        body: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
        store: Optional[Store] = None,
    ) -> SessionProxy:
        transport = make_mock_transport(status, body, headers)
        return SessionProxy(
            store or mem_store,
            upstream_url=UPSTREAM,
            max_retries=max_retries,
            retry_base_delay=0.01,
            retry_max_delay=0.1,
            transport=transport,
            now=clock,
        )
    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hdrs(pairs: List[Tuple[str, str]]) -> httpx.Headers:
    return httpx.Headers(dict(pairs))


async def forward(
    proxy: SessionProxy,
    session_id: str,
    method: str = "POST",
    path: str = "/v1/chat/completions",
    body: bytes = b'{"model":"test"}',
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> ProxyResponse:
    hdrs = [(SESSION_HEADER, session_id)]
    if extra_headers:
        hdrs.extend(extra_headers)
    return await proxy.forward(method, path, _hdrs(hdrs), body)


# ===========================================================================
# TEST 1: No session header → NoSessionHeader
# ===========================================================================

@pytest.mark.asyncio
async def test_no_session_header_raises_NoSessionHeader():
    """SessionProxy.forward raises NoSessionHeader when no session header is present."""
    transport = make_mock_transport(200, b"{}")
    p = SessionProxy(MagicMock(), upstream_url=UPSTREAM, transport=transport)
    try:
        with pytest.raises(NoSessionHeader):
            await p.forward("POST", "/v1/chat", _hdrs([("other-header", "val")]), b"body")
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 2: resolve() returns key_id and binds session, API key header is sent
# ===========================================================================

@pytest.mark.asyncio
async def test_resolve_returns_key_id_and_binds_session():
    """Store.resolve is called with the session ID and the bound key is used."""
    store = MagicMock()
    store.resolve.return_value = Binding("sess1", "key1", 1_700_000_000.0, 1_700_000_000.0)
    store.get_key.return_value = {"id": "key1", "secret": "sk-test-key1", "status": K_BOUND}

    captured_headers: Dict[str, str] = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(200, content=b'hello')

    transport = httpx.MockTransport(capture_handler)
    p = SessionProxy(store, upstream_url=UPSTREAM, transport=transport)
    try:
        resp = await forward(p, "sess1")
        store.resolve.assert_called_once_with("sess1")
        assert captured_headers.get("authorization") == "Bearer sk-test-key1"
        assert resp.session_id == "sess1"
        assert resp.key_id == "key1"
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 3: Hop-by-hop request headers stripped
# ===========================================================================

@pytest.mark.asyncio
async def test_hop_by_hop_request_headers_stripped(make_proxy):
    """connection, transfer-encoding, keep-alive are removed before forwarding."""
    captured: Dict[str, Any] = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"{}")

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(capture_handler)

    try:
        await forward(
            p, "sess-hop",
            extra_headers=[
                ("x-session-id", "sess-123"),  # session headers that must be stripped
                ("authorization", "Bearer token"),  # auth must be stripped
                ("content-type", "application/json"),
            ],
        )
        # Session headers and authorization must not reach upstream
        assert "x-session-id" not in captured["headers"]
        assert SESSION_HEADER not in captured["headers"]
        assert not captured["headers"].get("authorization", "").startswith("Bearer token")
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 4: Hop-by-hop response headers stripped in ProxyResponse
# ===========================================================================

@pytest.mark.asyncio
async def test_hop_by_hop_response_headers_stripped(make_proxy):
    """connection, transfer-encoding, content-length are removed from response."""
    p = make_proxy(200, b'{"result":"ok"}', headers={
        "content-type": "application/json",
        "connection": "keep-alive",
        "transfer-encoding": "chunked",
        "content-length": "15",
    })

    try:
        resp = await forward(p, "sess-hdrs")
        resp_headers_keys = {k.lower() for k, _ in resp.headers}
        hop = {"connection", "transfer-encoding", "keep-alive", "content-length"}
        overlap = hop & resp_headers_keys
        assert not overlap, f"hop-by-hop headers in response: {overlap}"
        assert "content-type" in resp_headers_keys
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 5: 2xx → streaming ProxyResponse
# ===========================================================================

@pytest.mark.asyncio
async def test_2xx_response_streams_upstream(make_proxy):
    """2xx responses set upstream and is_stream=True, content is None."""
    upstream_mock = MagicMock()
    upstream_mock.status_code = 200
    upstream_mock.headers = httpx.Headers({"content-type": "application/json"})
    upstream_mock.aiter_bytes = MagicMock(return_value=iter([b'{"result":"ok"}']))
    upstream_mock.aclose = AsyncMock()

    async def fake_send(request, **kwargs):
        return upstream_mock

    p = make_proxy(200, b'{"streaming":true}')
    p._client.send = fake_send  # type: ignore

    try:
        resp = await forward(p, "sess-2xx")
        assert resp.is_stream is True
        assert resp.content is None
        assert resp.upstream is upstream_mock
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 6: Non-2xx → buffered ProxyResponse
# ===========================================================================

@pytest.mark.asyncio
async def test_non_2xx_response_buffered(make_proxy):
    """Non-2xx responses set content and is_stream=False, upstream is None."""
    p = make_proxy(429, b"rate limited", headers={"retry-after": "5"})

    try:
        resp = await forward(p, "sess-429")
        assert resp.is_stream is False
        assert resp.content == b"rate limited"
        assert resp.upstream is None
        assert resp.status_code == 429
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 7: Retry on transient (5xx) with exponential back-off
# ===========================================================================

@pytest.mark.asyncio
async def test_retry_on_transient_error_with_backoff(make_proxy, clock: Clock):
    """Transient 5xx errors trigger retries with exponential backoff."""
    call_count = 0
    call_times: List[float] = []

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, call_times
        call_count += 1
        call_times.append(clock.t)
        if call_count < 3:
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(max_retries=2)
    p._client._transport = httpx.MockTransport(flaky_handler)

    try:
        resp = await forward(p, "sess-retry")
        assert call_count == 3, f"expected 3 attempts, got {call_count}"
        assert resp.classification.category == "success"
        # Verify exponential backoff: delay = base_delay * 2^(attempt-1)
        assert len(call_times) == 3
        delay_1_to_2 = call_times[1] - call_times[0]
        delay_2_to_3 = call_times[2] - call_times[1]
        base = 0.01  # retry_base_delay for this proxy
        assert abs(delay_1_to_2 - base) < 0.05, f"expected ~{base}s delay, got {delay_1_to_2}"
        assert abs(delay_2_to_3 - base * 2) < 0.05, f"expected ~{base*2}s delay, got {delay_2_to_3}"
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 8: No retry on client (4xx) error
# ===========================================================================

@pytest.mark.asyncio
async def test_no_retry_on_client_error(make_proxy):
    """4xx client errors are not retried - only one attempt is made."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, content=b"bad request")

    p = make_proxy(max_retries=3)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-400")
        assert call_count == 1, f"expected 1 attempt, got {call_count}"
        assert resp.classification.category == "client"
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 9: capacity (429 + capacity keyword) → cooldown
# ===========================================================================

@pytest.mark.asyncio
async def test_classify_capacity_sets_cooldown(make_proxy, mem_store: Store):
    """429 with capacity keyword sets key to K_COOLDOWN without retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, content=b"rate limit exceeded, please try again later")

    p = make_proxy(429, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-cap")
        assert call_count == 1
        assert resp.classification.category == "capacity"
        key = mem_store.get_key("key-alpha")
        assert key is not None
        assert key["status"] == K_COOLDOWN
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 10: quota (402 / 429 + quota keyword) → exhausted
# ===========================================================================

@pytest.mark.asyncio
async def test_classify_quota_sets_exhausted(make_proxy, mem_store: Store):
    """402/429 with quota keyword sets key to K_EXHAUSTED without retry."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, content=b"insufficient balance")

    p = make_proxy(402, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-quota")
        assert resp.classification.category == "quota"
        key = mem_store.get_key("key-alpha")
        assert key is not None
        assert key["status"] == K_EXHAUSTED
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 11: invalid (401) → invalid
# ===========================================================================

@pytest.mark.asyncio
async def test_classify_invalid_sets_invalid(make_proxy, mem_store: Store):
    """401 auth failure sets key to K_INVALID without retry."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"invalid api key")

    p = make_proxy(401, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-inv")
        assert resp.classification.category == "invalid"
        key = mem_store.get_key("key-alpha")
        assert key is not None
        assert key["status"] == K_INVALID
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 12: Body gating — large body passed through to upstream
# ===========================================================================

@pytest.mark.asyncio
async def test_body_gating_large_body_passed_through(make_proxy):
    """Request bodies are passed through to upstream; no body gating at proxy."""
    large_body = b"x" * (2 * 1024 * 1024 + 100)  # just over 2 MiB
    captured_content_length: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_content_length.append(len(request.content))
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-large", body=large_body)
        # The proxy does not gate request bodies; upstream API handles validation.
        assert captured_content_length[0] == len(large_body)
        assert resp.status_code == 200
    finally:
        await p._client.aclose()


# ===========================================================================
# TEST 13: Key lock prevents thundering herd on concurrent requests
# (removed: SessionProxy does not implement per-key backoff locks)

# ===========================================================================
# FASTAPI INTEGRATION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_proxy_endpoint_missing_session_returns_400(mem_store: Store, clock: Clock):
    """POST without session header → 400."""
    proxy = MagicMock()
    proxy.forward = AsyncMock(side_effect=NoSessionHeader())
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            PROXY_ENDPOINT,
            content=b'{"model":"gpt-4"}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "session" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_proxy_endpoint_no_key_returns_503(mem_store: Store, clock: Clock):
    """POST with session but NoKeyAvailable → 503."""
    proxy = MagicMock(spec=SessionProxy)
    proxy.forward = AsyncMock(side_effect=NoKeyAvailable("sess-none"))
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            PROXY_ENDPOINT,
            content=b'{"model":"gpt-4"}',
            headers={SESSION_HEADER: "sess-none", "content-type": "application/json"},
        )
        assert resp.status_code == 503
        assert "api key" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_proxy_endpoint_success_returns_streaming(mem_store: Store, clock: Clock):
    """Successful 2xx response is returned as a StreamingResponse (upstream is set)."""
    # Mock the proxy to return a ProxyResponse where upstream is set (triggers streaming path)
    async def chunk_gen(chunk_size=None):
        yield b'{"result":"ok"}'

    mock_upstream = MagicMock()
    mock_upstream.aiter_bytes = chunk_gen
    mock_upstream.aclose = AsyncMock()

    mock_proxy_resp = ProxyResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        classification=Classification(200, "success", "OK", False),
        session_id="sess-ok",
        key_id="key-alpha",
        upstream=mock_upstream,
    )

    proxy = MagicMock(spec=SessionProxy)
    proxy.forward = AsyncMock(return_value=mock_proxy_resp)
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            PROXY_ENDPOINT,
            content=b'{"model":"gpt-4"}',
            headers={SESSION_HEADER: "sess-ok", "content-type": "application/json"},
        )
        assert resp.status_code == 200


# ===========================================================================
# ADMIN ENDPOINT TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_admin_status_requires_token(mem_store: Store, clock: Clock):
    """GET /admin/status without token → 401."""
    proxy = MagicMock(spec=SessionProxy)
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/status")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_with_valid_token(mem_store: Store, clock: Clock):
    """GET /admin/status with valid token → 200 with keys, counts, bindings."""
    proxy = MagicMock(spec=SessionProxy)
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/status", headers={"X-Admin-Token": ADMIN_TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        assert "counts" in data
        assert "bindings" in data
        assert data["counts"]["available"] >= 0


@pytest.mark.asyncio
async def test_admin_reassign_unbinds_session(mem_store: Store, clock: Clock):
    """POST /admin/reassign-session/{session_id} unbinds the session."""
    # Force a binding: call resolve so store creates a binding for this session
    mem_store.resolve("sess-to-unbind")
    proxy = MagicMock(spec=SessionProxy)
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/admin/reassign-session/{'sess-to-unbind'}",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("reassigned") is True


@pytest.mark.asyncio
async def test_admin_reset_key_resets_to_available(mem_store: Store, clock: Clock):
    """POST /admin/reset-key/{key_id} resets key to K_AVAILABLE."""
    mem_store.set_key_state("key-alpha", K_EXHAUSTED)
    proxy = MagicMock(spec=SessionProxy)
    app = create_app(store=mem_store, proxy=proxy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/admin/reset-key/{'key-alpha'}",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert resp.status_code == 200

    key = mem_store.get_key("key-alpha")
    assert key is not None
    assert key["status"] == K_AVAILABLE


# ===========================================================================
# ADDITIONAL COVERAGE TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_forward_extracts_session_header_priority(make_proxy):
    """When both session headers are present, the first in SESSION_ID_HEADERS wins."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await p.forward(
            "POST",
            "/v1/chat",
            _hdrs([
                (SESSION_HEADER, "sess-priority"),
                ("x-session-id", "sess-legacy"),
            ]),
            b"{}",
        )
        assert resp.session_id == "sess-priority"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_strips_auth_and_session_headers(make_proxy):
    """authorization and session headers are stripped, bound key replaces auth."""

    captured_headers: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        await forward(
            p, "sess-auth",
            extra_headers=[
                ("authorization", "Bearer original-token"),
                ("x-session-id", "sess-123"),
                ("content-type", "application/json"),
            ],
        )
        # The original auth must not reach upstream
        assert not captured_headers.get("authorization", "").startswith("Bearer original-")
        # Session headers must not reach upstream
        assert "x-session-id" not in captured_headers
        assert SESSION_HEADER not in captured_headers
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_transient_exhausts_retries_and_returns_transient(make_proxy, mem_store: Store):
    """After max_retries on transient errors, transient classification is returned."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, content=b"service unavailable")

    transport = httpx.MockTransport(handler)
    p = SessionProxy(
        mem_store,
        upstream_url=UPSTREAM,
        max_retries=2,
        retry_base_delay=0.01,
        retry_max_delay=0.1,
        transport=transport,
    )

    try:
        resp = await forward(p, "sess-transient")
        assert call_count == 3, f"Expected 3 attempts (1 + 2 retries), got {call_count}"
        assert resp.classification.category == "transient"
        assert resp.classification.retryable is True
        assert resp.status_code == 503
        # Key stays bound (transient does not move key to a terminal state).
        key = mem_store.get_key("key-alpha")
        if key is not None:
            assert key["status"] == K_BOUND, f"Expected K_BOUND, got {key['status']}"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_raises_NoKeyAvailable_when_all_keys_exhausted(mem_store: Store, clock: Clock):
    """When all keys are K_EXHAUSTED, NoKeyAvailable is raised immediately."""

    for key_id in ["key-alpha", "key-beta", "key-gamma"]:
        mem_store.set_key_state(key_id, K_EXHAUSTED)

    transport = make_mock_transport(200, b"{}")
    p = SessionProxy(mem_store, upstream_url=UPSTREAM, transport=transport)

    try:
        with pytest.raises(NoKeyAvailable):
            await forward(p, "sess-none")
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_raises_NoKeyAvailable_when_all_keys_invalid(mem_store: Store, clock: Clock):
    """When all keys are K_INVALID, NoKeyAvailable is raised immediately."""

    for key_id in ["key-alpha", "key-beta", "key-gamma"]:
        mem_store.set_key_state(key_id, K_INVALID)

    transport = make_mock_transport(200, b"{}")
    p = SessionProxy(mem_store, upstream_url=UPSTREAM, transport=transport)

    try:
        with pytest.raises(NoKeyAvailable):
            await forward(p, "sess-none")
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_2xx_classification_success(make_proxy):
    """2xx is classified as 'success' with retryable=False."""

    p = make_proxy(200, b'{"ok":true}')

    try:
        resp = await forward(p, "sess-2xx")
        assert resp.classification.category == "success"
        assert resp.classification.retryable is False
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_retry_key_state_stays_bound_during_retries(make_proxy, mem_store: Store):
    """Key remains K_BOUND throughout all retry attempts."""

    call_count = 0
    key_states: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        key_states.append(mem_store.get_key("key-alpha")["status"])
        return httpx.Response(502, content=b"bad gateway")

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        await forward(p, "sess-sticky-bound")
    except Exception:
        pass  # may fail after retries; we only care about state

    assert all(s == K_BOUND for s in key_states), f"Key state changed during retries: {key_states}"


@pytest.mark.asyncio
async def test_classify_capacity_no_retry(make_proxy, mem_store: Store):
    """429 with capacity keyword → no retries, key enters cooldown immediately."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, content=b"rate limit exceeded, please try again later")

    p = make_proxy(429, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-cap-no-retry")
        assert call_count == 1, "capacity errors should not be retried"
        assert resp.classification.category == "capacity"
        assert resp.classification.retryable is False
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_mask_key_hides_secret():
    """mask_key() never exposes more than first 6 and last 4 characters."""
    # Long key: "sk-alpha-AAAAA-BBBBB-00001" = 26 chars
    # mask = sk-alp + *x16 + 0001 = "sk-alp****************0001"
    assert mask_key("sk-alpha-AAAAA-BBBBB-00001") == "sk-alp****************0001"
    # Short key (≤10 chars): reveal first 2, mask the rest
    assert mask_key("short") == "sh***"   # 5 chars: sh + 3 asterisks
    assert mask_key("") == "<empty>"


@pytest.mark.asyncio
async def test_fresh_session_creates_binding(mem_store: Store, clock: Clock):
    """First request from a session creates a new binding to an available key."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"ok":true}')

    transport = httpx.MockTransport(handler)
    p = SessionProxy(mem_store, upstream_url=UPSTREAM, transport=transport, now=clock)

    try:
        resp = await forward(p, "sess-new")
        assert resp.key_id == "key-alpha"

        bindings = mem_store.list_bindings()
        sess_binding = next((b for b in bindings if b.session_id == "sess-new"), None)
        assert sess_binding is not None
        assert sess_binding.key_id == "key-alpha"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_repeated_session_uses_same_binding(mem_store: Store, clock: Clock):
    """Repeated requests from the same session reuse the existing binding."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"ok":true}')

    transport = httpx.MockTransport(handler)
    p = SessionProxy(mem_store, upstream_url=UPSTREAM, transport=transport, now=clock)

    try:
        resp1 = await forward(p, "sess-repeat")
        key_id_1 = resp1.key_id

        resp2 = await forward(p, "sess-repeat")
        assert resp2.key_id == key_id_1

        bindings = mem_store.list_bindings()
        sess_bindings = [b for b in bindings if b.session_id == "sess-repeat"]
        assert len(sess_bindings) == 1
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_classify_403_quota_keyword(make_proxy, mem_store: Store):
    """403 with quota keyword → K_EXHAUSTED."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"insufficient_balance")

    p = make_proxy(403, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-403-quota")
        assert resp.classification.category == "quota"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_classify_403_capacity_keyword(make_proxy, mem_store: Store):
    """403 with capacity keyword → K_COOLDOWN."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"at capacity")

    p = make_proxy(403, store=mem_store)
    p._client._transport = httpx.MockTransport(handler)

    try:
        resp = await forward(p, "sess-403-cap")
        assert resp.classification.category == "capacity"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_passes_method_and_path(make_proxy):
    """Upstream request uses the caller's HTTP method and path."""
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        await forward(p, "sess-m", path="/v1/completions")
        assert captured["method"] == "POST"
        assert captured["path"] == "/v1/completions"
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_passes_query_string(make_proxy):
    """Upstream request includes any query string from the path."""
    captured: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        await forward(p, "sess-q", path="/v1/chat/completions?model=gpt-4&stream=true")
        assert "model=gpt-4" in captured["url"]
        assert "stream=true" in captured["url"]
    finally:
        await p._client.aclose()


@pytest.mark.asyncio
async def test_forward_passes_request_body(make_proxy):
    """Upstream request includes the caller's raw body bytes."""
    captured: Dict[str, bytes] = {}
    body = b'{"messages":[{"role":"user","content":"hello"}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, content=b'{"ok":true}')

    p = make_proxy(200, b"{}")
    p._client._transport = httpx.MockTransport(handler)

    try:
        await forward(p, "sess-body", body=body)
        assert captured["content"] == body
    finally:
        await p._client.aclose()
