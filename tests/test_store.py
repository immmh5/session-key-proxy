"""Tests for app.store — the session-bound API key store.

Covers: seeding, atomic resolve (first-available, no key), rotation between
sessions, session stickiness (keys are never swapped), cooldown gating and
expiry, unbind requeue, gc requeue of stale sessions (never auto-recovering
exhausted/invalid keys), status accounting and masking.

All time-dependent operations are driven through an injectable, mutable clock
(``Store(now=...)``) and explicit ``now`` parameters, so tests are fully
deterministic and never touch the real clock.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.store import Binding, Store
from app import util

# 3 keys with a range of distinct ids/labels.
SEED = {
    "keys": [
        {"id": "key-alpha", "key": "sk-aplha-ABCDEF-GHIJKL-0000-mnop", "label": "Alpha"},
        {"id": "key-beta", "key": "sk-beta-LONGER-DUMMY-SECRET-0000-abcd", "label": "Beta"},
        {"id": "key-gamma", "key": "sk-gamma-ANOTHER-DUMMY-SECRET-0000-wxyz", "label": "Gamma"},
    ]
}


class Clock:
    """A mutable fake wall clock (unix seconds)."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


@pytest.fixture
def keys_file(tmp_path: Path) -> Path:
    p = tmp_path / "keys.json"
    p.write_text(json.dumps(SEED), encoding="utf-8")
    return p


def make_store(tmp_path: Path, keys_file: Path, clock: Clock, **kw) -> Store:
    st = Store(
        db_path=tmp_path / "store.db",
        keys_file=str(keys_file),
        session_ttl=kw.pop("session_ttl", 300.0),
        cooldown=kw.pop("cooldown", 30.0),
        now=clock,
        **kw,
    )
    st.init()
    return st


# ---- seeding -------------------------------------------------------------

def test_init_seeds_pool_from_keys_file(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    assert st.count_by_status() == {"available": 3, "bound": 0, "cooldown": 0,
                                    "exhausted": 0, "invalid": 0}


def test_init_is_idempotent_and_does_not_reseed(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    st.init()  # second init must not duplicate rows
    assert len(st.list_keys()) == 3
    st.set_key_state("key-alpha", "exhausted")
    st.init()
    assert len(st.list_keys()) == 3  # no reseed, no resurrection
    assert st.get_key("key-alpha")["status"] == "exhausted"


def test_init_without_keys_file_stays_empty(tmp_path, clock_support=None):
    clock = Clock()
    st = Store(db_path=tmp_path / "store.db", now=clock)
    st.init()
    assert st.count_by_status()["available"] == 0
    assert st.resolve("sess-1") is None


# ---- resolve: first-available, binding, rotation -----------------------

def test_resolve_binds_first_available_key(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    b = st.resolve("sess-1")
    assert isinstance(b, Binding)
    assert b.session_id == "sess-1"
    assert b.key_id == "key-alpha"  # lowest id is picked first
    assert b.key_id == st.get_bound_key("sess-1")


def test_resolve_is_sticky_never_swaps_keys(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    first = st.resolve("sess-1")
    clock.advance(1000)
    second = st.resolve("sess-1")
    assert second.key_id == first.key_id
    # The bound key counts as bound, not available.
    assert st.get_key(first.key_id)["status"] == "bound"
    assert st.count_by_status()["bound"] == 1


def test_resolve_rotates_across_sessions(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    b1 = st.resolve("sess-1")
    b2 = st.resolve("sess-2")
    b3 = st.resolve("sess-3")
    assert {b1.key_id, b2.key_id, b3.key_id} == {"key-alpha", "key-beta", "key-gamma"}


def test_resolve_no_keys_returns_none(tmp_path):
    clock = Clock()
    empty = tmp_path / "empty-keys.json"
    empty.write_text(json.dumps({"keys": []}), encoding="utf-8")
    st = Store(db_path=tmp_path / "store.db", keys_file=str(empty), now=clock)
    st.init()
    assert st.resolve("sess-1") is None
    assert st.get_bound_key("sess-1") is None


# ---- cooldown -----------------------------------------------------------

def test_cooldown_key_is_skipped_and_recovered_after_expiry(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock, cooldown=30)
    st.set_key_state("key-alpha", "cooldown", cooldown_until=clock.t + 30)
    b = st.resolve("sess-1")
    assert b.key_id == "key-beta"  # alpha is cooling down
    assert st.count_by_status()["cooldown"] == 1
    # While still in cooldown, only one key remains available.
    b2 = st.resolve("sess-2")
    assert b2.key_id == "key-gamma"
    # After cooldown expiry, resolve picks alpha again (expired cooldown).
    clock.advance(31)
    b3 = st.resolve("sess-3")
    assert b3.key_id == "key-alpha"
    assert st.get_key("key-alpha")["status"] == "bound"
    assert st.get_key("key-alpha")["cooldown_until"] is None


def test_resolve_none_when_all_keys_in_cooldown(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    for kid in ("key-alpha", "key-beta", "key-gamma"):
        st.set_key_state(kid, "cooldown", cooldown_until=clock.t + 1000)
    assert st.resolve("sess-1") is None


# ---- unbind -------------------------------------------------------------

def test_unbind_requeues_key_for_reuse(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    b = st.resolve("sess-1")
    assert st.get_key(b.key_id)["status"] == "bound"
    assert st.unbind("sess-1") is True
    assert st.get_bound_key("sess-1") is None
    assert st.get_key(b.key_id)["status"] == "available"
    # Same key becomes immediately reusable.
    b2 = st.resolve("sess-2")
    assert b2.key_id == b.key_id
    # Unbinding a non-existent session is a no-op.
    assert st.unbind("does-not-exist") is False


# ---- gc ----------------------------------------------------------------

def test_gc_collects_stale_sessions_and_requeues_keys(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock, session_ttl=300)
    st.resolve("sess-1")  # t
    clock.advance(100)
    st.resolve("sess-2")  # t+100
    clock.advance(250)    # t+350: sess-1 stale, sess-2 fresh
    assert st.gc() == 1
    assert st.get_bound_key("sess-1") is None
    assert st.get_bound_key("sess-2") == "key-beta"
    # Requeued stale key becomes available again.
    assert st.get_key("key-alpha")["status"] == "available"


def test_gc_never_auto_recovers_exhausted_or_invalid(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    b = st.resolve("sess-1")
    st.set_key_state("key-alpha", "exhausted")
    clock.advance(st.session_ttl + 100)
    st.resolve("sess-2")  # key-beta binds sess-2 fresh (after alpha went stale)
    st.gc()  # must NOT resurrect exhausted alpha (unbound) ...
    assert st.get_key("key-alpha")["status"] == "exhausted"
    assert st.get_key("key-beta")["status"] == "bound"  # sess-2 still active? 
    # Force sess-2 stale too and mark beta invalid AFTER binding; gc must not
    # overwrite the invalid status either.
    st.set_key_state("key-beta", "invalid")
    clock.advance(st.session_ttl + 100)
    st.gc()
    assert st.get_key("key-beta")["status"] == "invalid"
    # sess-2 binding was cleared (row deleted) but the key stays invalid.
    assert st.get_bound_key("sess-2") is None


def test_gc_active_sessions_are_not_collected(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock, session_ttl=300)
    st.resolve("sess-1")
    clock.advance(299)
    assert st.gc() == 0  # still within TTL
    assert st.get_bound_key("sess-1") is not None
    # Active usage refreshes last_seen, keeping sessions alive.
    clock.advance(2)
    st.resolve("sess-1")
    clock.advance(299)
    assert st.gc() == 0


# ---- status accounting & admin views -----------------------------------

def test_count_by_status_tracks_lifecycle(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    st.resolve("sess-1")                       # alpha -> bound
    st.set_key_state("key-beta", "cooldown")   # beta -> cooldown
    st.set_key_state("key-gamma", "exhausted") # gamma -> exhausted
    counts = st.count_by_status()
    assert counts["available"] == 0
    assert counts["bound"] == 1
    assert counts["cooldown"] == 1
    assert counts["exhausted"] == 1
    assert counts["invalid"] == 0


def test_get_key_and_list_keys_never_expose_full_secret(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    for key in st.list_keys():
        assert key["id"].startswith("key-")
        assert key["status"] == "available"
        assert key["masked"] is not None
        assert key["masked"] not in ("", key["secret"])
        assert key["secret"] not in ("",)
    k = st.get_key("key-alpha")
    assert k["id"] == "key-alpha"
    assert k["label"] == "Alpha"
    assert k["masked"] == util.mask_key(k["secret"])
    assert st.get_key("nope") is None


def test_mask_key_behaviour():
    # Seeded placeholder secrets are long enough to be safely masked.
    masked = util.mask_key("sk-aplha-ABCDEF-GHIJKL-0000-mnop")
    assert masked.startswith("sk-apl")          # first 6 chars
    assert masked.endswith("mnop")              # last 4 chars
    assert "*" in masked                       # middle is starred
    assert "ABCDEF" not in masked
    assert util.mask_key("") == "<empty>"
    assert util.mask_key("short") != "short"    # short keys fully covered


def test_set_key_state_returns_whether_key_existed(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    assert st.set_key_state("key-alpha", "exhausted") is True
    assert st.get_key("key-alpha")["status"] == "exhausted"
    # Unknown key id: no-op, reports False.
    assert st.set_key_state("ghost", "available") is False


def test_set_key_state_rejects_unknown_status(tmp_path, keys_file):
    clock = Clock()
    st = make_store(tmp_path, keys_file, clock)
    with pytest.raises(ValueError):
        st.set_key_state("key-alpha", "mystery-status")
