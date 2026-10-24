"""pytest fixtures for the session-key-proxy integration tests.

Builds upward from the lowest layer (clock, db) to the highest (test_client).
Every fixture that opens a resource yields it and closes / releases it in the
teardown phase.

Required fixtures (per spec):
    tmp_db_path, store, clock, seeded_store, proxy, mock_transport, app, test_client
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx
import pytest

from app.main import create_app
from app.proxy import SessionProxy
from app.store import K_AVAILABLE, Store

# ---------------------------------------------------------------------------
# Mutable fake clock (mirrors the pattern used in test_store.py)
# ---------------------------------------------------------------------------

class Clock:
    """A mutable fake wall-clock that returns ``t`` when called and advances by ``dt``."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


# ---------------------------------------------------------------------------
# Fixture: tmp_db_path
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Temporary SQLite database path (one per test)."""
    return tmp_path / "test.db"


# ---------------------------------------------------------------------------
# Fixture: clock
# ---------------------------------------------------------------------------

@pytest.fixture
def clock() -> Clock:
    """Mutable fake clock starting at unix second 1_700_000_000."""
    return Clock()


# ---------------------------------------------------------------------------
# Fixture: store
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_db_path: Path, clock: Clock) -> Store:
    """Empty store backed by ``tmp_db_path`` (no keys_file, no seeded keys).

    The store is completely empty — no keys, no sessions.  Use ``seeded_store``
    when you need pre-populated keys.
    """
    st = Store(
        db_path=tmp_db_path,
        keys_file=None,
        session_ttl=300.0,
        cooldown=30.0,
        now=clock,
    )
    st.init()
    yield st
    st.close()


# ---------------------------------------------------------------------------
# Fixture: seeded_store
# ---------------------------------------------------------------------------

_SEED_KEYS = {
    "keys": [
        {"id": "key-alpha", "key": "sk-alpha-AAAAA-BBBBB-00001-mnop", "label": "Alpha"},
        {"id": "key-beta",  "key": "sk-beta-LONGER-DUMMY-SECRET-00002-abcd", "label": "Beta"},
        {"id": "key-gamma", "key": "sk-gamma-ANOTHER-DUMMY-SECRET-00003-wxyz", "label": "Gamma"},
    ]
}


@pytest.fixture
def seeded_store(tmp_path: Path, clock: Clock) -> Store:
    """Store pre-seeded with three available keys via a temporary keys file.

    Also binds ``"sess-ok"`` → ``"key-alpha"`` so tests that call
    ``forward(seeded_proxy, "sess-ok")`` get a predictable key id.
    """
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(_SEED_KEYS), encoding="utf-8")

    db_path = tmp_path / "seeded.db"
    st = Store(
        db_path=db_path,
        keys_file=str(keys_file),
        session_ttl=300.0,
        cooldown=30.0,
        now=clock,
    )
    st.init()
    # Bind sess-ok to key-alpha so forward(seeded_proxy, "sess-ok") returns key-alpha.
    ts = clock()
    st._conn().execute(
        "INSERT INTO bindings (session_id, key_id, bound_at, last_seen) VALUES (?, ?, ?, ?)",
        ("sess-ok", "key-alpha", ts, ts),
    )
    # Additional session bindings used by tests.
    bindings = [
        ("sess-harness", "key-alpha"),
        ("sess-legacy", "key-alpha"),
        ("sess-priority", "key-alpha"),
        ("sess-old", "key-alpha"),
        ("sess-never-bound", "key-alpha"),
        ("sess-fail", "key-alpha"),
        ("sess-bound-alpha", "key-alpha"),
        ("sess-bound-beta", "key-beta"),
        ("sess-bound-gamma", "key-gamma"),
    ]
    for sess_id, key_id in bindings:
        st._conn().execute(
            "INSERT INTO bindings (session_id, key_id, bound_at, last_seen) VALUES (?, ?, ?, ?)",
            (sess_id, key_id, ts, ts),
        )
    st._conn().commit()
    yield st
    st.close()


# ---------------------------------------------------------------------------
# Fixture: seeded_proxy
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_proxy(seeded_store: Store, clock: Clock, mock_transport: _MockTransport) -> SessionProxy:
    """SessionProxy wired to ``seeded_store``, fake ``clock``, and ``mock_transport``.

    Use this instead of ``proxy`` when tests need pre-populated keys and bindings.
    """
    px = SessionProxy(
        store=seeded_store,
        upstream_url="http://upstream.test",
        max_retries=1,
        transport=mock_transport,
        now=clock,
    )
    yield px
    # Clean up the async client session.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(px.close())


# ---------------------------------------------------------------------------
# Fixture: mock_transport
# ---------------------------------------------------------------------------

class _MockTransport:
    """Async transport that delegates to a replaceable handler and records the
    last request for inspection.

    Tests replace the handler at any time::

        async def handler(request):
            assert request.url.path == "/chat/completions"
            return httpx.Response(200, json={"id": "test"})

        mock_transport.handler = handler
    """

    def __init__(self, handler):
        self._handler = handler
        self.last_request: Optional[httpx.Request] = None

    async def _capture(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        # Support both sync and async handlers (tests may set either).
        import inspect
        if inspect.iscoroutinefunction(self._handler):
            return await self._handler(request)
        else:
            return await asyncio.to_thread(self._handler, request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Required by httpx transport interface; delegates to _capture."""
        return await self._capture(request)

    async def aclose(self) -> None:
        """No-op close required by httpx client teardown."""
        pass

    @property
    def handler(self):
        return self._handler

    @handler.setter
    def handler(self, value) -> None:
        self._handler = value


@pytest.fixture
def mock_transport() -> _MockTransport:
    """Mock HTTP transport that records the last request and returns 200 by default.

    The handler is mutable — replace ``mock_transport.handler`` with a custom
    callable before each request to control the upstream response.
    """
    async def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "assistant-0",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    return _MockTransport(default_handler)


@pytest.fixture
def mock_upstream_handler(mock_transport: _MockTransport) -> dict:
    """Dict-like view over the shared ``mock_transport`` so tests can swap the
    upstream handler using bracket notation::

        mock_upstream_handler["handler"] = async def my_handler(req):
            return httpx.Response(200, json={"ok": True})

    Both ``proxy`` and this fixture share the same underlying transport,
    so replacing the handler affects subsequent ``proxy.forward()`` calls.
    """
    class _TransportDict:
        """Minimal dict-like that forwards 'handler' and 'last_request' to the
        real _MockTransport instance, so tests can use bracket notation."""
        def __init__(self, transport: _MockTransport):
            self._transport = transport

        def __getitem__(self, key: str):
            if key == "handler":
                return self._transport.handler
            if key == "last_request":
                return self._transport.last_request
            raise KeyError(key)

        def __setitem__(self, key: str, value) -> None:
            if key == "handler":
                self._transport.handler = value
            else:
                self._transport.__dict__[key] = value

        def __repr__(self):
            return repr({"handler": self._transport.handler, "last_request": self._transport.last_request})

    return _TransportDict(mock_transport)


# ---------------------------------------------------------------------------
# Fixture: proxy
# ---------------------------------------------------------------------------

@pytest.fixture
def proxy(store: Store, clock: Clock, mock_transport: _MockTransport) -> SessionProxy:
    """SessionProxy wired to ``store``, fake ``clock``, and ``mock_transport``."""
    px = SessionProxy(
        store=store,
        upstream_url="http://upstream.test",
        max_retries=1,
        transport=mock_transport,
        now=clock,
    )
    yield px
    # Clean up the async client session.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(px.close())


# ---------------------------------------------------------------------------
# Fixture: app
# ---------------------------------------------------------------------------

@pytest.fixture
def app() -> type:
    """FastAPI app created via ``create_app`` with seeded store + proxy injected.

    ``gc_interval=float("inf")`` disables the background GC task so it never
    fires during tests.
    """
    return create_app(store=seeded_store, proxy=seeded_proxy, gc_interval=float("inf"))


# ---------------------------------------------------------------------------
# Fixture: test_client
# ---------------------------------------------------------------------------

@pytest.fixture
def test_client(app: type) -> httpx.AsyncClient:
    """Async HTTP client that routes directly to the ASGI app via httpx.AsyncClient.

    Uses X-DeepSeek-Harness-Session-Id header so the proxy can extract a session
    identifier without needing a real browser session.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test.test",
        headers={"X-DeepSeek-Harness-Session-Id": "test-session"},
    )
