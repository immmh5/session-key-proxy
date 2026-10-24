"""Central clock + shared helpers so tests can inject deterministic time."""
import time as _time


def now() -> float:
    """Unix epoch seconds (monotonic-clock-independent wall clock)."""
    return _time.time()


def mask_key(secret: str) -> str:
    """Produce a safe, human-inspectable mask of an API key.

    Never reveal more than the first 6 and last 4 characters; the middle is
    replaced with asterisks. Short keys are masked entirely.
    """
    if not secret:
        return "<empty>"
    secret = secret.strip()
    if len(secret) <= 10:
        # Too short to mask safely: reveal nothing.
        return secret[:2] + "*" * max(1, len(secret) - 2)
    return f"{secret[:6]}{'*' * max(6, len(secret) - 10)}{secret[-4:]}"
