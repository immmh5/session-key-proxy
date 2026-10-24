"""Session-Bound API Key Proxy for DeepSeek Harness.

Binds one API key per unique session ID, persists bindings in a
session-bound store, and never silently swaps keys between sessions.
"""

__version__ = "1.0.0"
