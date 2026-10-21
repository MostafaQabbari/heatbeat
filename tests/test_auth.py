"""Tests for passwords, tokens and roles (src/heatbeat/auth.py)."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from heatbeat.auth import InvalidToken, Role, User, create_token, decode_token, hash_password, verify_password

SECRET = "test-secret-0123456789abcdef-0123456789"
USER = User(id=7, tenant_id=3, email="op@example.test", role=Role.OPERATOR)


def test_password_round_trip():
    stored = hash_password("correct horse")

    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)


def test_same_password_gets_a_different_hash_each_time():
    """A random salt: equal passwords do not produce equal hashes."""
    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize("stored", ["", "plain-text", "md5$abc", "scrypt$broken"])
def test_garbage_hash_never_verifies(stored):
    assert verify_password("anything", stored) is False


def test_token_round_trip():
    user = decode_token(create_token(USER, SECRET, timedelta(minutes=5)), SECRET)

    assert user == USER


def test_expired_token_is_rejected():
    issued = datetime.now(UTC) - timedelta(hours=2)
    token = create_token(USER, SECRET, timedelta(minutes=5), now=issued)

    with pytest.raises(InvalidToken):
        decode_token(token, SECRET)


def test_token_signed_with_another_secret_is_rejected():
    token = create_token(USER, "another-secret-0123456789abcdef-012345", timedelta(minutes=5))

    with pytest.raises(InvalidToken):
        decode_token(token, SECRET)


def test_tampered_role_is_rejected():
    """Changing the payload (viewer -> admin) breaks the signature."""
    header, _payload, signature = create_token(USER, SECRET, timedelta(minutes=5)).split(".")
    forged_payload = jwt.utils.base64url_encode(b'{"role":"admin"}').decode()

    with pytest.raises(InvalidToken):
        decode_token(f"{header}.{forged_payload}.{signature}", SECRET)


def test_unsigned_token_alg_none_is_rejected():
    """The classic attack: a token that says 'alg: none' must never be accepted."""
    claims = {"iss": "heatbeat", "sub": "1", "tid": 1, "role": "admin", "iat": 1, "exp": 9_999_999_999}
    unsigned = jwt.encode(claims, key=None, algorithm="none")

    with pytest.raises(InvalidToken):
        decode_token(unsigned, SECRET)


def test_token_without_tenant_is_rejected():
    claims = {"iss": "heatbeat", "sub": "1", "role": "admin", "iat": 1, "exp": 9_999_999_999}

    with pytest.raises(InvalidToken):
        decode_token(jwt.encode(claims, SECRET, algorithm="HS256"), SECRET)


def test_roles_are_ordered():
    assert Role.ADMIN.allows(Role.OPERATOR)
    assert Role.OPERATOR.allows(Role.VIEWER)
    assert not Role.VIEWER.allows(Role.OPERATOR)
    assert not Role.OPERATOR.allows(Role.ADMIN)
