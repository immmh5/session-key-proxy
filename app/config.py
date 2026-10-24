"""Configuration for the Session-Bound API Key Proxy.

All values can be overridden via environment variables. Defaults are tuned
for a free-tier (Render) deployment: background tasks can spin down, so
persistence is file-based SQLite (no external service required).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-admin-token")
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.deepseek.com/v1/chat/completions")

# Endpoint the harness will actually call. Defaults to the proxy base.
# The FastAPI route is defined on this exact path (proxy.py).
PROXY_ENDPOINT = os.environ.get("PROXY_ENDPOINT", "/v1/chat/completions")

# File-based SQLite store (session-bound persistence). WAL mode.
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "proxy.db")))

# Seed file listing the pool. Required at first startup.
KEYS_FILE = Path(os.environ.get("KEYS_FILE", str(BASE_DIR / "keys.json")))

# Bindings are permanent once the session binds a key, but we still GC the
# session row after this TTL so orphaned sessions don't grow the table.
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30)))  # 30 days

# A key enters cooldown for this long after a 429/Capacity signal.
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "30"))

# Retries for transient (5xx / network) failures on the SAME key.
MAX_TRANSIENT_RETRIES = int(os.environ.get("MAX_TRANSIENT_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "0.5"))
RETRY_MAX_DELAY = float(os.environ.get("RETRY_MAX_DELAY", "8.0"))

# Admin listeners.
STATUS_PATH = os.environ.get("STATUS_PATH", "/admin/status")
RESET_KEY_PATH = os.environ.get("RESET_KEY_PATH", "/admin/reset-key/{key_id}")
ADD_KEY_PATH = os.environ.get("ADD_KEY_PATH", "/admin/add-key")
UPDATE_KEY_PATH = os.environ.get("UPDATE_KEY_PATH", "/admin/update-key/{key_id}")
DELETE_KEY_PATH = os.environ.get("DELETE_KEY_PATH", "/admin/delete-key/{key_id}")
UNBIND_SESSION_PATH = os.environ.get("UNBIND_SESSION_PATH", "/admin/unbind/{session_id}")
RENAME_KEY_PATH = os.environ.get("RENAME_KEY_PATH", "/admin/rename-key/{key_id}")
REASSIGN_PATH = os.environ.get("REASSIGN_PATH", "/admin/reassign-session/{session_id}")

# Session id header names, in priority order.
SESSION_ID_HEADERS = ("x-deepseek-harness-session-id", "x-session-id")

# Proxy strips these before forwarding upstream and injects the bound key.
STRIP_HEADERS = ("x-deepseek-harness-session-id", "x-session-id", "authorization")

# Gate on the CRITICAL section only (raw upstream body may be large).
MAX_BUFFER_FOR_BODY = int(os.environ.get("MAX_BUFFER_FOR_BODY", str(2 * 1024 * 1024)))  # 2 MiB
