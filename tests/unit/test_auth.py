from __future__ import annotations

import time

import jwt
import pytest

from apps.shared.auth import (
    AuthError,
    Principal,
    bearer_token,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from tests.conftest import TEST_JWT_SECRET


def test_token_roundtrip() -> None:
    tok = issue_token(Principal(42, "doctor"), TEST_JWT_SECRET, ttl_s=60)
    p = verify_token(tok, TEST_JWT_SECRET)
    assert p == Principal(42, "doctor")


def test_token_claims_follow_protocol() -> None:
    now = 1_700_000_000
    tok = issue_token(Principal(7, "patient"), TEST_JWT_SECRET, ttl_s=3600, now=now)
    header = jwt.get_unverified_header(tok)
    payload = jwt.decode(tok, options={"verify_signature": False})
    assert header["alg"] == "HS256"
    assert payload == {
        "sub": "7",
        "role": "patient",
        "typ": "access",
        "iat": now,
        "exp": now + 3600,
    }


def test_wrong_secret_rejected() -> None:
    tok = issue_token(Principal(1, "doctor"), TEST_JWT_SECRET, ttl_s=60)
    with pytest.raises(AuthError):
        verify_token(tok, "another-secret-" + "y" * 40)


def test_tampered_payload_rejected() -> None:
    tok = issue_token(Principal(1, "doctor"), TEST_JWT_SECRET, ttl_s=60)
    head, payload, sig = tok.split(".")
    forged = jwt.encode({"sub": "999", "iat": 1, "exp": 9_999_999_999}, "x" * 40)
    with pytest.raises(AuthError):
        verify_token(".".join([head, forged.split(".")[1], sig]), TEST_JWT_SECRET)


def test_expired_rejected() -> None:
    tok = issue_token(Principal(1, "doctor"), TEST_JWT_SECRET, ttl_s=1, now=time.time() - 100)
    with pytest.raises(AuthError):
        verify_token(tok, TEST_JWT_SECRET)


def test_alg_none_rejected() -> None:
    tok = jwt.encode(
        {"sub": "1", "iat": int(time.time()), "exp": int(time.time()) + 60}, None, algorithm="none"
    )
    with pytest.raises(AuthError):
        verify_token(tok, TEST_JWT_SECRET)


def test_missing_sub_rejected() -> None:
    now = int(time.time())
    tok = jwt.encode({"iat": now, "exp": now + 60}, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_token(tok, TEST_JWT_SECRET)


def test_non_positive_ttl() -> None:
    with pytest.raises(ValueError):
        issue_token(Principal(1, "doctor"), TEST_JWT_SECRET, ttl_s=0)


def test_password_hash_verify() -> None:
    enc = hash_password("bench-1", b"salt-1", iterations=1000)
    assert verify_password("bench-1", enc)
    assert not verify_password("bench-2", enc)
    assert not verify_password("bench-1", "garbage")
    assert not verify_password("bench-1", "md5$1$a$b")


def test_password_hash_params_validated() -> None:
    with pytest.raises(ValueError):
        hash_password("p", b"", 1000)
    with pytest.raises(ValueError):
        hash_password("p", b"s", 0)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer "])
def test_bearer_parse_errors(header: str | None) -> None:
    with pytest.raises(AuthError):
        bearer_token(header)


def test_bearer_parse_ok() -> None:
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
