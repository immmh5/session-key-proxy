"""Upstream API error classification for the DeepSeek proxy.

Given the raw HTTP status code (and optionally the response body) from the
upstream API, :func:`classify` decides how the proxy should treat that
response. The returned :class:`Classification` carries a ``category`` the
proxy switches on, a human-readable ``message`` it may surface to the client,
and a ``retryable`` flag.

Categories:
    - ``"capacity"``  -> key goes into cooldown (rate limit / capacity
      pressure); retry later on the SAME key.
    - ``"quota"``     -> key is exhausted / billing problem; PERSISTENT
      (never retried).
    - ``"invalid"``   -> the key is bad (401 auth); PERSISTENT.
    - ``"transient"`` -> upstream hiccup (5xx / network); retry on the SAME
      key (``retryable=True``).
    - ``"client"``    -> bad request from the caller; forward to client
      as-is, no state change.
    - ``"success"``   -> 2xx.

Pure standard library only; no external dependencies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class Classification:
    """The immutable result of classifying one upstream response."""

    status_code: int
    category: str  # "capacity" | "quota" | "invalid" | "transient" | "success" | "client"
    message: str  # human-readable; the proxy may surface it to the client.
    retryable: bool  # True only when category == "transient".


# --- Keyword lexicons for the 403 hybrid case -------------------------------
# Each list of substrings is matched against the lowercased body text. A
# keyword only counts as evidence if the words "no" / "not" do NOT appear in
# the 80 characters immediately before the match (constrained negation), so
# phrases like "no quota" or "not authorized" are never treated as evidence.
QUOTA_WORDS = [
    "insufficient balance",
    "insufficient_balance",
    "insufficient",
    "quota",
    "balance",
    "billing",
    "billing quota exhausted",
    "payment",
]

CAPACITY_WORDS = [
    "capacity",
    "rate limit",
    "rate_limit",
    "too many requests",
    "temporarily",
    "busy",
    "overloaded",
    "overloaded.",
    "no available",
    "at capacity",
    "limit reached",
    "please try again later",
    "try again later",
    "service is overloaded",
]

INVALID_WORDS = [
    "invalid api key",
    "api key",
    "authentication",
    "unauthorized",
    "no such token",
    "invalid access token",
    "wrong api",
    "api key is invalid",
]

# How many characters of trailing text before a keyword are checked for a
# negation ("no" / "not") before a hit is counted.
_NEGATION_WINDOW = 80

# Words that flip a nearby keyword into a non-hit.
_NEGATIONS = ("no", "not")

# Insufficiency idioms: when a negation word appears together with one of these
# phrases right before a keyword, the match is a REAL deficiency signal (e.g.
# "you do not have enough balance") and must still count as evidence rather than
# being suppressed by the generic negation rule.
_INSUFFICIENCY_IDIOMS = (
    "do not have enough",
    "does not have enough",
    "not have enough",
    "not enough",
    "do not have sufficient",
    "does not have sufficient",
    "not have sufficient",
    "not sufficient",
)


def _is_insufficiency(window: str) -> bool:
    """True when ``window`` contains a negation powering a deficiency idiom."""
    if not any(neg in window for neg in _NEGATIONS):
        return False
    return any(idiom in window for idiom in _INSUFFICIENCY_IDIOMS)


def _body_text(body: Optional[Union[bytes, str]]) -> str:
    """Decode a raw body to a lowercased string, or "" when absent/empty."""
    if not body:
        return ""
    if isinstance(body, bytes):
        # Try UTF-8; fall back to latin-1 so we never hard-crash decoding.
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            body = body.decode("latin-1", errors="replace")
    return body.lower().strip()


def _keyword_hits(text: str, keywords) -> int:
    """Count keyword hits in ``text`` under constrained negation.

    A keyword occurrence is suppressed when "no"/"not" appears in the 80
    characters immediately before the match -- UNLESS that negation is part of
    an insufficiency idiom (e.g. "you do not have enough balance"), which is
    genuine evidence and still counts. The same keyword may match multiple
    times; each independent match is counted.
    """
    hits = 0
    search_from = 0
    for keyword in keywords:
        search_from = 0
        while True:
            idx = text.find(keyword, search_from)
            if idx < 0:
                break
            window_start = max(0, idx - _NEGATION_WINDOW)
            window = text[window_start:idx]
            if not any(neg in window for neg in _NEGATIONS):
                hits += 1
            elif _is_insufficiency(window):
                # "do not have enough balance" is a real deficiency signal.
                hits += 1
            search_from = idx + 1  # allow overlapping occurrences
    return hits


def _score_403(body_text: str):
    """Score a 403 body across the three classes; None selected the tie-break."""
    quota = _keyword_hits(body_text, QUOTA_WORDS)
    capacity = _keyword_hits(body_text, CAPACITY_WORDS)
    invalid = _keyword_hits(body_text, INVALID_WORDS)
    scores = {"quota": quota, "capacity": capacity, "invalid": invalid}
    best = max(scores.values())
    best_count = list(scores.values()).count(best)
    if best == 0:
        return None  # no positive evidence at all -> "client"
    # Ties resolve to "capacity" per spec.
    if best_count > 1:
        return "capacity"
    return next(cls for cls, count in scores.items() if count == best)


def _extract_json_message(body: Optional[Union[bytes, str]]) -> Optional[str]:
    """Pull ``{"error": {"message": ...}}`` or ``{"message": ...}`` text.

    Returns the raw message string (``None`` if not found or not JSON).
    Handles bytes, str, and str-encoded JSON.
    """
    if not body:
        return None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            body = body.decode("latin-1", errors="replace")
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        import json

        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Prefer {"error": {"message": ...}}.
    err = data.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return err["message"]
    if isinstance(data.get("message"), str):
        return data["message"]
    return None


# Concise default messages per category.
_DEFAULT_MESSAGES = {
    "capacity": "Upstream rate-limited / at capacity (429)",
    "quota": "Upstream quota exhausted (402)",
    "invalid": "Invalid API key (401)",
    "transient": "Upstream transient error (5xx)",
    "success": "",
    "client": "Invalid request",
}


def classify(
    status_code: int, body: Optional[Union[bytes, str]] = None
) -> Classification:
    """Classify an upstream response by its status code and body.

    Status is applied FIRST; only the 403 hybrid case consults the body.

    Args:
        status_code: The raw HTTP status code from the upstream API.
        body: The (possibly absent) raw response body, as bytes or str.

    Returns:
        An immutable :class:`Classification` describing how to treat the
        response.
    """
    # 1. Success.
    if 200 <= status_code <= 299:
        return Classification(status_code, "success", "", False)

    # 2. Rate limit / capacity pressure -> cooldown on the SAME key.
    #    429 with quota/billing keywords in body = quota (not retried).
    if status_code == 429:
        body_txt = _body_text(body)
        if body_txt and _keyword_hits(body_txt, QUOTA_WORDS) > 0:
            return Classification(status_code, "quota", _DEFAULT_MESSAGES["quota"], False)
        return Classification(status_code, "capacity", _DEFAULT_MESSAGES["capacity"], False)

    # 3. Billing / quota exhausted -> PERSISTENT (never retried).
    if status_code == 402:
        return Classification(status_code, "quota", _DEFAULT_MESSAGES["quota"], False)

    # 4. Bad key (auth) -> PERSISTENT.
    if status_code == 401:
        return Classification(status_code, "invalid", _DEFAULT_MESSAGES["invalid"], False)

    # 5. Upstream hiccup -> retry on the SAME key.
    if 500 <= status_code <= 599:
        return Classification(status_code, "transient", _DEFAULT_MESSAGES["transient"], True)

    # 6. 403 hybrid: decide by keyword scoring (quota / capacity / invalid).
    if status_code == 403:
        category = _score_403(_body_text(body)) or "client"
        json_message = _extract_json_message(body)
        message = json_message or _DEFAULT_MESSAGES.get(category, _DEFAULT_MESSAGES["client"])
        return Classification(status_code, category, message, False)

    # 7. Everything else (400, 404, 409, ...) -> client.
    message = _extract_json_message(body) or _DEFAULT_MESSAGES["client"]
    return Classification(status_code, "client", message, False)
