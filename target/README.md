# Session-Bound API Key Proxy for DeepSeek Harness

**Session-bound: each DeepSeek session gets ONE persistent key.** A FastAPI + httpx + SQLite (WAL) proxy that binds a single DeepSeek API key to each unique session id, isolates failures per session, and exposes admin endpoints for key/session management.

> **Session-bound:** one session id → one key, forever (until an admin explicitly assigns a new key). No silent swapping.

---

## 1. Why / the problem it solves

The DeepSeek Harness normally sends **one shared API key across all sessions**. That has a couple of serious failure modes:

- **A single bad key tanks every session.** One key that hits quota, gets rate-limited, enters cooldown, or is revoked silently breaks *every* user session at the same moment. There is no isolation, and no way to know which key failed.
- **Retrying doesn't help** if the failures are tied to that key (quota/ban) rather than transient load. Retrying the same key just compounds the problem.
- **Silently swapping keys** under the hood is dangerous — a session's load would land on someone else's key, rewriting the semantics of the request and violating the "one session, one key" contract.

**The fix:** bind one key per session id. When a key for a session is chosen, it is **persisted** in the SQLite store. From then on every request from that session uses that same key — until an admin explicitly reassigns it. No silent swapping, ever.

---

## 2. How it works (concise)

```
request
  -> extract session id (x-deepseek-harness-session-id, fallback x-session-id)
  -> Store resolves (or binds) the key for that session id
  -> forward the request upstream via httpx (DeepSeek API)
  -> classify the upstream response (see tables below)
  -> update the key's state in the Store, return the upstream response
```

**Key state machine**

| State | Meaning | Entered when |
|-------|---------|--------------|
| `available` | Free to be bound to a new session | startup / admin reset |
| `bound` | Assigned to one session id; serves that session only | first request from that session |
| `cooldown` | Temporarily parked after rate-limit/capacity signals | 429 / 529 / capacity |
| `exhausted` | Quota permanently used up; never auto-reused | 402 / 403 with quota reason |
| `invalid` | Key rejected / revoked; never auto-reused | 401 / 403 (invalid credentials) |

**Error classification**

| Upstream status | Meaning | Proxy action |
|-----------------|---------|--------------|
| `429` / capacity (`529`) | Rate limited / overloaded | mark key **cooldown** (30s, `COOLDOWN_SECONDS`) |
| `402` or `403` with quota reason | Quota exhausted | mark key **exhausted** (permanent) |
| `401` or `403` (invalid) | Bad / revoked key | mark key **invalid** (permanent) |
| `5xx` / network error | Server/transient failure | retry **the SAME key** up to `MAX_TRANSIENT_RETRIES` (3) with exponential backoff (`RETRY_BASE_DELAY` 0.5s → `RETRY_MAX_DELAY` 8s) |
| other `4xx` | Request problem | return to the client unchanged |

**The never-silently-swap guarantee.** Once a session is bound to a key, the proxy never moves it to another key on its own — even on failure. A stuck session (key exhausted/invalid/cooldown) stays bound to that key until an **admin** explicitly calls `POST /admin/reassign-session/{session_id}`. This keeps failure isolation predictable and auditable.

---

## 3. Request/response flow

1. DeepSeek Harness (or any client) POSTs to the proxy's `PROXY_ENDPOINT` (default `/v1/chat/completions`) with a **session-identifying header**.
2. The proxy extracts the session id: `x-deepseek-harness-session-id` (primary), falling back to `x-session-id`.
3. The `Store` looks up the key already bound to that session id; if none, it binds an `available` key (persisted in SQLite).
4. The request is forwarded verbatim to `UPSTREAM_URL` through httpx.
5. The upstream response is classified, the key state stored, and the response returned to the client.

**Client-set headers (all OPTIONAL, but recommended)**

| Header | Purpose |
|--------|---------|
| `x-deepseek-harness-session-id` | Primary session identifier. Each unique value gets one bound key. |
| `x-session-id` | Fallback session identifier, used if the primary header is absent. |

Send at least one of these. If neither is present, the proxy treats the request as unauthenticated-by-session (the exact behaviour is defined in the app; for correct per-session key binding always send `x-deepseek-harness-session-id`).

---

## 4. API reference

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `POST` | `/v1/chat/completions` | Proxy to DeepSeek (the route the harness POSTs to) | session header |
| `GET` | `/admin/status` | Key pool + session binding snapshot | `X-Admin-Token` |
| `POST` | `/admin/reset-key/{key_id}` | Re-mark a key `available` (clears cooldown/exhausted/invalid) | `X-Admin-Token` |
| `POST` | `/admin/reassign-session/{session_id}` | Force a session onto a freshly chosen `available` key | `X-Admin-Token` |
| `GET` | `/healthz` | Liveness/health probe (used by Render + Docker healthcheck) | none |

All `/admin/*` endpoints require the `X-Admin-Token` header set to `ADMIN_TOKEN`.

### `POST /v1/chat/completions`

Proxy endpoint — pass a normal DeepSeek chat-completions JSON body. The session id is taken from `x-deepseek-harness-session-id` (fallback `x-session-id`).

### `GET /admin/status`

```bash
curl -s http://localhost:8000/admin/status \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

Sample (masked) output — key secrets are **always masked** as `first6****last4`:

```json
{
  "keys": [
    { "id": "key-alpha", "state": "bound",   "masked": "sk-1234****9abc", "bound_session": "sess_9f2c1" },
    { "id": "key-beta",  "state": "available", "masked": "sk-5678****def0", "bound_session": null },
    { "id": "key-gamma", "state": "cooldown",  "masked": "sk-abcd****1234", "bound_session": null }
  ],
  "sessions": [
    { "id": "sess_9f2c1", "key_id": "key-alpha", "last_seen": "2025-01-01T00:00:00Z" }
  ]
}
```

**Keys are always masked.** The raw DeepSeek key never appears in any admin response.

### `POST /admin/reset-key/{key_id}`

Re-arms a key after cooldown/exhausted/invalid so it can be bound again:

```bash
curl -s -X POST http://localhost:8000/admin/reset-key/key-alpha \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

### `POST /admin/reassign-session/{session_id}`

Move a stuck session to a freshly bound `available` key:

```bash
curl -s -X POST http://localhost:8000/admin/reassign-session/sess_9f2c1 \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

### `GET /healthz`

```bash
curl -s http://localhost:8000/healthz
```

Returns a simple OK once the app is up.

---

## 5. Local development

**Prereqs:** Python 3.14, `uv`.

```bash
# 1. Env file
cp env.example .env
#    IMPORTANT: the canonical env var for the key-pool seed file is KEYS_FILE.
#    In env.example the old name was KEY_POOL_FILE — the app reads KEYS_FILE,
#    so set that (e.g. KEYS_FILE=./keys.json). See "Configuration reference".

# 2. Virtualenv + deps
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements.txt

# 3. Seed the key pool with REAL DeepSeek keys
#    Edit keys.json: {"keys":[{"id","key","label"}]}. The proxy reads it only
#    to seed a fresh (empty) DB, so put real `sk-...` values in BEFORE first boot.
#    The three placeholders are DEMO strings — they will not work upstream.

# 4. (Optional) point DB_PATH at its default <project>/data/proxy.db
#    export DB_PATH=./data/proxy.db   # default anyway

# 5. Set a strong admin token
export ADMIN_TOKEN="$(openssl rand -hex 32)"

# 6. Run
uvicorn app.main:app --reload --port 8000
```

Then point the DeepSeek Harness at:

```
http://localhost:8000/v1/chat/completions
```

with the header `x-deepseek-harness-session-id: <your-session-id>`.

---

## 6. Configuration reference

All configuration is environment-driven and optional (every var has a default). The canonical names below are the ones the app (`app/config.py`) actually reads.

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Port uvicorn binds. Render injects `$PORT` automatically and the Dockerfile `CMD` falls back to 8000. |
| `ADMIN_TOKEN` | `change-me-admin-token` | Required by all `/admin/*` routes. **Set a strong random value** (Render can generate one via `generateValue: true`). |
| `UPSTREAM_URL` | `https://api.deepseek.com/v1/chat/completions` | Upstream DeepSeek endpoint the proxy forwards to. |
| `PROXY_ENDPOINT` | `/v1/chat/completions` | Route on this proxy that the harness POSTs to. |
| `DB_PATH` | `<project>/data/proxy.db` | SQLite store file location. On Render: `/var/data/proxy.db` (disk-mounted). |
| `KEYS_FILE` | `<project>/keys.json` | Seed file for the key pool, read only on a fresh (empty) DB. This is the canonical name — `env.example` may still mention the old `KEY_POOL_FILE`; the app reads **`KEYS_FILE`**. |
| `SESSION_TTL_SECONDS` | `2592000` (30 days) | Session rows are GC'd after this much inactivity; live bindings remain permanent while a session is active. |
| `COOLDOWN_SECONDS` | `30` | Seconds a key is parked after a 429/capacity signal. |
| `MAX_TRANSIENT_RETRIES` | `3` | Transient (5xx/network) retries on the SAME key. |
| `RETRY_BASE_DELAY` | `0.5` | Initial exponential-backoff delay (seconds). |
| `RETRY_MAX_DELAY` | `8.0` | Maximum backoff delay (seconds). |
| `MAX_BUFFER_FOR_BODY` | `2 MiB` | Max buffered request body for proxy handling. |

**Session headers** (as above): `x-deepseek-harness-session-id` (primary) and `x-session-id` (fallback).

---

## 7. Deploy to Render (free tier)

1. **Push the repo** (including `Dockerfile`, `render.yaml`, `app/`, `keys.json`, `requirements.txt`) to GitHub/GitLab.
2. **Create the Blueprint:** in the Render Dashboard → *New* → *Blueprint*, pick the repo. Render reads `render.yaml`, creates the `session-key-proxy` web service (docker runtime) and its 1 GB disk.
3. **Set a strong `ADMIN_TOKEN`** — either let Render generate it (`generateValue: true` in the Blueprint) or set it manually in the Dashboard. **Read the generated value from the Dashboard > Environment** — you will not see it again.
4. **The disk is mounted automatically** by the Blueprint at `/var/data`; `DB_PATH=/var/data/proxy.db` points there.
5. **Seed `keys.json` BEFORE first boot.** The proxy seeds the DB from `KEYS_FILE` only when the DB is empty. Replace the three placeholders in `keys.json` with your **real** DeepSeek keys and push **before** the first deploy — a fresh deploy after the DB exists will not re-seed.
6. **Verify env vars:** `UPSTREAM_URL`, `KEYS_FILE=/app/keys.json`, `DB_PATH=/var/data/proxy.db`, `PORT=8000` (Render also injects `$PORT` for web services).
7. **Deploy** and wait; the health check hits `GET /healthz`.
8. **Point the harness** at `https://<your-service>.onrender.com/v1/chat/completions` with the `x-deepseek-harness-session-id` header.

> **Don't forget your real keys.** The repo ships three `sk-DEMO-PLACEHOLDER-...` keys that will fail upstream. Seeding happens once — put real keys in before the first boot.

**Free-tier note:** Render free instances spin down after ~15 min of inactivity and cold-start on the next request. An [UptimeRobot](https://uptimerobot.com) headless HTTP monitor set against `https://<your-service>.onrender.com/healthz` every 5 minutes keeps it awake.

---

## 8. Security notes

- **Configure `ADMIN_TOKEN`.** The default `change-me-admin-token` is unsafe — set a strong random value (or let Render generate one) and keep it out of git (add it to the Dashboard's env vars, or a `.env` that is git-ignored).
- **Keys are masked.** Admin responses never expose raw DeepSeek keys; they are shown as `first6****last4` only.
- **Session header required for binding.** Per-session isolation works only when the client sends `x-deepseek-harness-session-id` (or `x-session-id`). Without it the proxy cannot bind a per-session key.
- **The disk holds the DB.** `DB_PATH` lives on the mounted disk — the SQLite store (with key-state + session bindings) is the only data that persists; there is no managed database.
- **`keys.json` in the repo is a seed, not a secret store** — the raw key material sits in the image at `/app/keys.json`. For stricter hygiene, rotate keys via `/admin/reset-key` / `/admin/reassign-session` and treat `keys.json` as placeholder-only after first boot.

---

## 9. Testing

```bash
python -m pytest
```

The test suite exercises the proxy with `httpx` `MockTransport`/`ASGITransport` — **no live network**, no real DeepSeek calls, and no real API keys needed. Tests cover classification of upstream responses (429/402/401/5xx/4xx), per-session key binding, admin endpoints, and the never-silently-swap guarantee. A generated test key file is used; your real `keys.json` is not touched. (Requires a writable temp `DB_PATH`.)

---

## 10. License

MIT — do whatever you like, but no warranty is implied. See the upstream DeepSeek API terms for key usage.
