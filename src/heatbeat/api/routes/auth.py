"""Login and "who am I".

What this file does: exchanges e-mail + password for a signed access token
(OAuth2 password flow, so the "Authorize" button in /docs works), and returns
the current user. Wrong e-mail and wrong password give the same answer and take
the same time, so nobody can find out which accounts exist.
"""

from datetime import timedelta
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from psycopg_pool import ConnectionPool

from heatbeat.api.deps import CurrentUser, get_pool, get_settings
from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import Me, Problem, Token
from heatbeat.auth import DUMMY_HASH, Role, User, create_token, verify_password
from heatbeat.config import Settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRepository(Protocol):
    def find(self, email: str) -> tuple[User, str] | None: ...


class PostgresLoginRepository:
    """Runs as the table owner: a login happens before we know the tenant."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def find(self, email: str) -> tuple[User, str] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, tenant_id, email, role, password_hash FROM app_user WHERE email = %s",
                (email.strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        user_id, tenant_id, mail, role, password_hash = row
        return User(id=user_id, tenant_id=tenant_id, email=mail, role=Role(role)), password_hash


def get_login_repository(pool: Annotated[ConnectionPool, Depends(get_pool)]) -> LoginRepository:
    return PostgresLoginRepository(pool)


@router.post("/token", response_model=Token, responses={401: {"model": Problem}})
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    repo: Annotated[LoginRepository, Depends(get_login_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Token:
    """Log in with e-mail (as `username`) and password."""
    found = repo.find(form.username)
    stored = found[1] if found else DUMMY_HASH
    password_ok = verify_password(form.password, stored)  # always runs: same timing either way
    if found is None or not password_ok:
        raise ApiProblem(401, "Wrong e-mail or password", headers={"WWW-Authenticate": "Bearer"})
    assert settings.jwt_secret is not None
    ttl = timedelta(minutes=settings.jwt_ttl_minutes)
    token = create_token(found[0], settings.jwt_secret.get_secret_value(), ttl)
    return Token(access_token=token, expires_in=int(ttl.total_seconds()))


@router.get("/me", response_model=Me, responses={401: {"model": Problem}})
def me(user: CurrentUser) -> Me:
    return Me(id=user.id, email=user.email, role=user.role.value, tenant_id=user.tenant_id)
