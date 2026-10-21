"""Shared request dependencies: who is calling, may they, and a tenant-scoped DB connection.

What this file does:
- `current_user`: reads the Bearer token and returns the user (401 if missing or invalid)
- `require(role)`: 403 unless the user has at least that role
- `rate_limited`: 429 when this user's tenant used up its request budget (S17)
- `tenant_db`: a connection inside a transaction that runs as the restricted
  role `heatbeat_api` with `app.tenant_id` set, so the DATABASE enforces
  tenant isolation even if a query forgets its filter (S12)
"""

import math
from collections.abc import Callable, Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from psycopg_pool import ConnectionPool

from heatbeat.api.problems import ApiProblem
from heatbeat.auth import InvalidToken, Role, User, decode_token
from heatbeat.config import Settings
from heatbeat.ratelimit import TokenBucketLimiter

oauth2 = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_pool(request: Request) -> ConnectionPool:
    pool: ConnectionPool = request.app.state.pool
    return pool


def current_user(
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(oauth2)],
) -> User:
    challenge = {"WWW-Authenticate": "Bearer"}
    if not token:
        raise ApiProblem(401, "Not authenticated", "Send 'Authorization: Bearer <token>'", challenge)
    assert settings.jwt_secret is not None
    try:
        return decode_token(token, settings.jwt_secret.get_secret_value())
    except InvalidToken as exc:
        raise ApiProblem(401, "Invalid or expired token", headers=challenge) from exc


CurrentUser = Annotated[User, Depends(current_user)]


def require(role: Role) -> Callable[[User], User]:
    def check(user: CurrentUser) -> User:
        if not user.role.allows(role):
            raise ApiProblem(403, "Forbidden", f"This needs the role '{role}' or higher")
        return user

    return check


def rate_limited(request: Request, user: CurrentUser) -> None:
    limiter: TokenBucketLimiter = request.app.state.limiter
    wait = limiter.acquire(user.tenant_id)
    if wait > 0:
        raise ApiProblem(
            429,
            "Too many requests",
            "Request budget for your organisation is used up",
            {"Retry-After": str(math.ceil(wait))},
        )


def tenant_db(pool: Annotated[ConnectionPool, Depends(get_pool)], user: CurrentUser) -> Iterator[psycopg.Connection]:
    with pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE heatbeat_api")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(user.tenant_id),))
        yield conn


TenantDb = Annotated[psycopg.Connection, Depends(tenant_db)]
