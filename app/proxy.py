"""HTTP forwarding layer: session-id → bound API key → upstream.

SessionProxy owns the httpx.AsyncClient and the retry/classification loop.
It is pure async so it stays unit-testable with httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from . import config
from .classification import Classification, classify as classify_response
from .store import K_AVAILABLE, K_COOLDOWN, K_EXHAUSTED, K_INVALID, Binding, Store
from .util import mask_key, now as _now

logger = logging.getLogger(__name__)

# Categories considered retryable — transient (5xx / network) failures only.
_RETRYABLE_CATEGORIES = ("transient",)

# Hop-by-hop headers that must not be forwarded.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})


class NoSessionHeader(ValueError):
    """Raised when no recognised session-id header is present."""


class NoKeyAvailable(RuntimeError):
    """Raised when resolve() cannot return a binding (pool exhausted)."""


@dataclass
class ProxyResponse:
    """Result of SessionProxy.forward()."""

    status_code: int
    headers: List[Tuple[str, str]]           # forwardable upstream headers
    classification: Classification
    session_id: str
    key_id: str
    # Exactly one of the following is set:
    content: Optional[bytes] = None         # buffered body (non-2xx)
    upstream: Optional[httpx.AsyncResponse] = None  # open streaming response (2xx)

    @property
    def is_stream(self) -> bool:
        return self.upstream is not None


class SessionProxy:
    def __init__(
        self,
        store: Store,
        *,
        upstream_url: str = config.UPSTREAM_URL,
        max_retries: int = config.MAX_TRANSIENT_RETRIES,
        retry_base_delay: float = config.RETRY_BASE_DELAY,
        retry_max_delay: float = config.RETRY_MAX_DELAY,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        now=None,
    ):
        self.store = store
        self.upstream_url = upstream_url
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self._now = now or _now

        # Derive origin from UPSTREAM_URL so that relative paths are resolved correctly.
        parsed = httpx.URL(upstream_url)
        self._base_url = httpx.URL(path="/", scheme=parsed.scheme, host=parsed.host, port=parsed.port)

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release the underlying client connection pool."""
        await self._client.aclose()

    async def forward(
        self,
        method: str,
        path: str,
        headers: httpx.Headers,
        body: Optional[bytes],
    ) -> ProxyResponse:
        """Handle one proxied request end-to-end.

        1. Extract session id from incoming headers.
        2. Resolve the immutable binding (creates / refreshes atomically).
        3. Strip sensitive headers, inject the bound API key.
        4. Execute the request with transient retry (exponential backoff).
        5. Classify the response; update key state for capacity/quota/invalid.
        6. Return a ProxyResponse — streaming for 2xx, buffered for non-2xx.
        """
        session_id = self.extract_session_id(headers)
        if session_id is None:
            raise NoSessionHeader(
                f"No session header found in {list(config.SESSION_ID_HEADERS)}"
            )

        binding = self.store.resolve(session_id)
        if binding is None:
            raise NoKeyAvailable(f"No API key available for session {session_id!r}")

        key = self.store.get_key(binding.key_id)
        if key is None:
            # Key was deleted between resolve and now — fail fast.
            raise NoKeyAvailable(f"Key {binding.key_id!r} vanished for session {session_id!r}")

        upstream_headers = self._build_upstream_headers(headers, key["secret"])

        request = self._client.build_request(
            method,
            path,
            headers=upstream_headers,
            content=body,
        )

        classification: Optional[Classification] = None
        content: Optional[bytes] = None
        upstream_resp: Optional[httpx.AsyncResponse] = None
        last_resp: Optional[httpx.AsyncResponse] = None
        attempt = 0

        while True:
            attempt += 1
            try:
                last_resp = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                # Network / connection error — treat as transient, retry if eligible.
                if attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                # Exhausted retries: return a synthetic 502.
                classification = classify_response(502, b"upstream unreachable")
                break

            # Success / non-success status.
            if 200 <= last_resp.status_code <= 299:
                # 2xx → stream immediately to client; no classification needed.
                upstream_resp = last_resp
                classification = classify_response(last_resp.status_code, b"")
                break

            # Non-2xx: buffer up to the cap, close the stream, then classify.
            content = await self._read_capped(last_resp)
            await last_resp.aclose()
            classification = classify_response(last_resp.status_code, content)

            if classification.category in _RETRYABLE_CATEGORIES and attempt <= self.max_retries:
                await self._backoff(attempt)
                continue

            # Persistent error (or retries exhausted) — stop looping.
            break

        # Apply state signals for known terminal categories.
        self._apply_signal(binding.key_id, classification)

        # Extract forwardable response headers before returning.
        resp_headers = (
            self._filter_response_headers(dict(last_resp.headers))
            if last_resp is not None
            else []
        )

        if upstream_resp is not None:
            return ProxyResponse(
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                classification=classification,
                session_id=session_id,
                key_id=binding.key_id,
                upstream=upstream_resp,
            )
        else:
            return ProxyResponse(
                status_code=classification.status_code,
                headers=resp_headers,
                classification=classification,
                session_id=session_id,
                key_id=binding.key_id,
                content=content or b"",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_session_id(headers: httpx.Headers) -> Optional[str]:
        """Return the first session id found in SESSION_ID_HEADERS, or None."""
        for name in config.SESSION_ID_HEADERS:
            value = headers.get(name)
            if value:
                return value
        return None

    @staticmethod
    def _build_upstream_headers(
        incoming: httpx.Headers,
        secret: str,
    ) -> Dict[str, str]:
        """Select and clean headers before forwarding to upstream.

        Strips session id headers, Authorization (replaced by our Bearer token),
        and hop-by-hop headers.
        """
        out: Dict[str, str] = {}
        for name, value in incoming.items():
            lname = name.lower()
            if lname in config.STRIP_HEADERS or lname in _HOP_BY_HOP:
                continue
            out[name] = value

        if secret:
            out["Authorization"] = f"Bearer {secret}"
        return out

    @staticmethod
    def _filter_response_headers(headers: Dict[str, str]) -> List[Tuple[str, str]]:
        """Remove hop-by-hop and oversized headers before forwarding downstream."""
        result: List[Tuple[str, str]] = []
        for name, value in headers.items():
            lname = name.lower()
            if lname in _HOP_BY_HOP:
                continue
            # content-length is invalid when body is streamed/regenerated.
            if lname == "content-length":
                continue
            result.append((name, value))
        return result

    async def _backoff(self, attempt: int) -> None:
        """Sleep for exponential-backoff delay before the next retry attempt."""
        delay = min(
            self.retry_base_delay * (2 ** (attempt - 1)),
            self.retry_max_delay,
        )
        logger.debug("backing off %.1fs before retry %d", delay, attempt)
        await asyncio.sleep(delay)

    async def _read_capped(
        self,
        response: httpx.AsyncResponse,
    ) -> bytes:
        """Read the response body up to MAX_BUFFER_FOR_BODY bytes."""
        cap = config.MAX_BUFFER_FOR_BODY
        body = b""
        async for chunk in response.aiter_bytes(chunk_size=65536):
            body += chunk
            if len(body) >= cap:
                logger.debug(
                    "body capped at %d bytes (original larger)", cap
                )
                break
        return body[:cap]

    def _apply_signal(self, key_id: str, classification: Classification) -> None:
        """Update key state in the store based on classification category."""
        ts = self._now()
        cat = classification.category

        if cat == "capacity":
            # 429 → put the key in cooldown so other sessions can't grab it.
            self.store.set_key_state(
                key_id,
                K_COOLDOWN,
                cooldown_until=ts + self.store.cooldown,
            )
            logger.info(
                "key %s hit capacity limit, cooldown until %s",
                mask_key(key_id),
                self._now_to_iso(ts + self.store.cooldown),
            )

        elif cat == "quota":
            # 402 → mark exhausted (persistent, admin must reset).
            self.store.set_key_state(key_id, K_EXHAUSTED)
            logger.warning("key %s exhausted (quota exceeded)", mask_key(key_id))

        elif cat == "invalid":
            # 401 → mark invalid (persistent, admin must reset).
            self.store.set_key_state(key_id, K_INVALID)
            logger.warning("key %s invalid (auth failure)", mask_key(key_id))

    @staticmethod
    def _now_to_iso(ts: float) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
