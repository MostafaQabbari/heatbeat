"""FastAPI application.

What this file does: builds the app (create_app): settings, database pool,
rate limiter, CORS for the Vue portal, request ids and metrics, all routers,
health endpoints and one error format (problem+json). It also writes the
OpenAPI document to docs/openapi.json, the versioned API documentation.

Run it with:  uv run heatbeat-api      then open http://127.0.0.1:8000/docs
"""

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

from heatbeat.api import problems
from heatbeat.api.problems import ApiProblem
from heatbeat.api.routes import alarms, auth, commands, insights, plants, recommendations
from heatbeat.api.schemas import Problem
from heatbeat.config import Settings
from heatbeat.observability import install_http_metrics
from heatbeat.ratelimit import TokenBucketLimiter

OPENAPI_FILE = Path("docs/openapi.json")
DOCS_SETTINGS = {"db_password": "unused", "jwt_secret": "x" * 32}


def create_app(settings: Settings | None = None, connect_db: bool = True) -> FastAPI:
    settings = settings or Settings()
    secret = settings.jwt_secret.get_secret_value() if settings.jwt_secret else ""
    if len(secret) < 32:
        raise ValueError("HB_JWT_SECRET must be set and at least 32 characters long")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if connect_db:
            app.state.pool = ConnectionPool(
                settings.database_url,
                min_size=1,
                max_size=10,
                open=True,
                kwargs={"options": "-c timezone=UTC"},
            )
        yield
        if connect_db:
            app.state.pool.close()

    app = FastAPI(
        title="Heatbeat API",
        version="1.0.0",
        description=(
            "Monitoring and control of heating plants. All times are UTC (ISO 8601). "
            "Log in at POST /v1/auth/token, then send `Authorization: Bearer <token>`."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.limiter = TokenBucketLimiter(settings.rate_limit_per_minute)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )
    install_http_metrics(app)
    problems.install(app)
    for module in (auth, plants, alarms, commands, insights, recommendations):
        app.include_router(module.router)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness: the process is up (does not touch the database)."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], responses={503: {"model": Problem}})
    def ready(request: Request) -> dict[str, str]:
        """Readiness: the database answers."""
        try:
            with request.app.state.pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception as exc:
            raise ApiProblem(503, "Database unavailable") from exc
        return {"status": "ready"}

    return app


def openapi_json() -> str:
    app = create_app(Settings(_env_file=None, **DOCS_SETTINGS), connect_db=False)  # type: ignore[arg-type]
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def write_openapi() -> None:
    """Regenerate docs/openapi.json (a test fails if it is out of date)."""
    OPENAPI_FILE.parent.mkdir(exist_ok=True)
    OPENAPI_FILE.write_text(openapi_json(), encoding="utf-8", newline="\n")
    print(f"wrote {OPENAPI_FILE}", file=sys.stderr)


def run() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run("heatbeat.api.main:create_app", factory=True, host=settings.api_host, port=settings.api_port)
