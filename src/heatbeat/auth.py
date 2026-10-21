"""Passwords, access tokens and roles.

What this file does: hashes passwords with scrypt (Python standard library),
issues and checks signed JWT access tokens (HS256, algorithm pinned, expiry
required), and defines the three roles viewer < operator < admin.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt

ISSUER = "heatbeat"
ALGORITHM = "HS256"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


class Role(StrEnum):
    VIEWER = "viewer"  # read only
    OPERATOR = "operator"  # + request parameter changes, acknowledge alarms
    ADMIN = "admin"  # + approve risky changes, manage alarm rules

    @property
    def rank(self) -> int:
        return {"viewer": 1, "operator": 2, "admin": 3}[self.value]

    def allows(self, required: "Role") -> bool:
        return self.rank >= required.rank


@dataclass(frozen=True)
class User:
    id: int
    tenant_id: int
    email: str
    role: Role


class InvalidToken(Exception):
    """The token is missing, expired, tampered with or signed with another key."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest))
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate, _unb64(digest))  # constant time


# Used when the e-mail is unknown, so a login takes the same time either way
# (an attacker cannot find out which e-mail addresses exist).
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def create_token(user: User, secret: str, ttl: timedelta, now: datetime | None = None) -> str:
    issued = now or datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "sub": str(user.id),
        "tid": user.tenant_id,
        "email": user.email,
        "role": user.role.value,
        "iat": issued,
        "exp": issued + ttl,
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> User:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # never trust the algorithm named in the token header
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "tid", "role"]},
        )
        return User(
            id=int(claims["sub"]),
            tenant_id=int(claims["tid"]),
            email=str(claims.get("email", "")),
            role=Role(claims["role"]),
        )
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise InvalidToken(str(exc)) from exc
