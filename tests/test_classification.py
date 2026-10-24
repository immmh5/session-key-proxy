"""Unit tests for app/classification.py.

Table-driven: each case asserts BOTH the category and the retryable flag.
Run with: python -m pytest tests/test_classification.py
"""
import pytest

from app.classification import Classification, classify


CASES = [
    # --- 429 -> capacity ------------------------------------------------------
    (
        429,
        '{"error":{"message":"rate limit reached, retry later"}}',
        "capacity",
    ),
    (429, "Too many requests", "capacity"),
    (429, b"Too many requests", "capacity"),
    (429, None, "capacity"),
    # --- 402 -> quota --------------------------------------------------------
    (402, '{"error":{"message":"Insufficient Balance"}}', "quota"),
    # --- 401 -> invalid ------------------------------------------------------
    (401, '{"error":{"message":"invalid api key"}}', "invalid"),
    (401, None, "invalid"),
    # --- 5xx -> transient, retryable -----------------------------------------
    (500, "internal server error", "transient"),
    (502, "bad gateway", "transient"),
    (503, '{"error":{"message":"service unavailable"}}', "transient"),
    (502, None, "transient"),
    (599, None, "transient"),
    # --- 2xx -> success ------------------------------------------------------
    (200, '{"id":"x","message":"ok"}', "success"),
    (204, None, "success"),
    (299, "", "success"),
    # --- 403 hybrid: quota ---------------------------------------------------
    (
        403,
        '{"error":{"message":"You do not have enough balance"}}',
        "quota",
    ),
    # "not enough"/"not have enough" idioms must still signal deficiency.
    (403, '{"error":{"message":"Your plan does not have enough balance"}}', "quota"),
    (403, '{"error":{"message":"Not enough quota remaining for this request"}}', "quota"),
    (403, "Your account does not have sufficient balance to continue", "quota"),
    (403, '{"error":{"message":"insufficient balance, please top up"}}', "quota"),
    (
        403,
        '{"error":{"message":"Your account quota has been exhausted"}}',
        "quota",
    ),
    (
        # "no"/"not" is far from "quota": quota must still count.
        403,
        '{"error":{"message":"Your account quota has been exhausted, not supported"}}',
        "quota",
    ),
    # --- 403 hybrid: capacity ------------------------------------------------
    (
        403,
        '{"error":{"message":"The server is currently overloaded with other '
        'requests and cannot process this request"}}',
        "capacity",
    ),
    (403, "please try again later", "capacity"),
    (403, "The server is overloaded, try again later", "capacity"),
    # --- 403 hybrid: no evidence -> client -----------------------------------
    (403, "Your key is not authorized", "client"),  # only "not authorized" hits
    (403, "", "client"),
    (403, None, "client"),
    (403, "There is no quota limit on your current plan", "client"),  # "no quota"
    (403, "Your plan has no balance credit", "client"),  # "no balance" not an idiom
    (403, "  ", "client"),
    # --- 400 and other -> client ---------------------------------------------
    (400, '{"error":{"message":"Bad request"}}', "client"),
    (400, '{"error":{"message":"unsupported parameter"}}', "client"),
    (404, "not found", "client"),
    (409, None, "client"),
    (429 - 1, None, "client"),  # 428, unspecified status
]


class TestClassification:
    @pytest.mark.parametrize("status,body,expected", CASES)
    def test_category_and_retryable(self, status, body, expected):
        c = classify(status, body)
        assert isinstance(c, Classification)
        assert c.status_code == status
        assert c.category == expected
        assert c.retryable is (expected == "transient")

    def test_immutable_frozen(self):
        c = classify(429, "x")
        with pytest.raises(Exception):
            c.category = "quota"  # type: ignore[misc]

    def test_message_unsupported_parameter(self):
        c = classify(400, '{"error":{"message":"unsupported parameter"}}')
        assert "unsupported parameter" in c.message

    def test_message_not_authorized_is_client(self):
        # A 403 with only negated evidence is classified client; make sure we
        # never guess quota/invalid from "not authorized".
        c = classify(403, "Your key is not authorized")
        assert c.category == "client"

    def test_bytes_body_supported(self):
        c = classify(429, "Too many requests".encode("utf-8"))
        assert c.category == "capacity"

    def test_message_empty_on_success(self):
        c = classify(200, '{"message":"hi"}')
        assert c.message == ""

    def test_default_messages(self):
        assert classify(429, None).message == "Upstream rate-limited / at capacity (429)"
        assert classify(402, None).message == "Upstream quota exhausted (402)"
        assert classify(401, None).message == "Invalid API key (401)"
        assert classify(500, None).message == "Upstream transient error (5xx)"
        assert classify(418, None).message == "Invalid request"
