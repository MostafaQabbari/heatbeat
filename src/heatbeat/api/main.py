"""FastAPI application.

What this file does: builds the app (create_app), opens the database
connection pool on startup, adds health endpoints, and turns every error into
the same application/problem+json format. Also writes the OpenAPI document to
docs/openapi.json, the versioned API documentation.

Run it with:  uv run heatbeat-api      then open http://localhost:8000/docs
"""

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException

from heatbeat.api.repository import PlantRepository
from heatbeat.api.routes import ApiProblem, get_repository, router
from heatbeat.api.schemas import Problem
from heatbeat.config import Settings

PROBLEM_JSON = "application/problem+json"
OPENAPI_FILE = Path("docs/openapi.json")


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(problem.model_dump(), status_code=problem.status, media_type=PROBLEM_JSON)


def create_app(settings: Settings | None = None, connect_db: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if connect_db:
            url = (settings or Settings()).database_url
            app.state.pool = ConnectionPool(
                url, min_size=1, max_size=10, open=True, kwargs={"options": "-c timezone=UTC"}
            )
        yield
        if connect_db:
            app.state.pool.close()

    app = FastAPI(
        title="Heatbeat API",
        version="1.0.0",
        description="Read API for heating-plant monitoring. All times are UTC (ISO 8601).",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness: the process is up (does not touch the database)."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], responses={503: {"model": Problem}})
    def ready(repo: PlantRepository = Depends(get_repository)) -> dict[str, str]:  # noqa: B008
        """Readiness: the database answers."""
        try:
            repo.ping()
        except Exception as exc:
            raise ApiProblem(503, "Database unavailable") from exc
        return {"status": "ready"}

    @app.exception_handler(ApiProblem)
    async def on_api_problem(_req: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(exc.problem)

    @app.exception_handler(HTTPException)
    async def on_http_error(_req: Request, exc: HTTPException) -> JSONResponse:
        return problem_response(Problem(status=exc.status_code, title=str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_req: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", []))
        return problem_response(
            Problem(status=422, title="Invalid request", detail=f"{where}: {first['msg']}")
        )

    return app


def openapi_json() -> str:
    return json.dumps(create_app(connect_db=False).openapi(), indent=2, sort_keys=True) + "\n"


def write_openapi() -> None:
    """Regenerate docs/openapi.json (CI fails if it is out of date)."""
    OPENAPI_FILE.parent.mkdir(exist_ok=True)
    OPENAPI_FILE.write_text(openapi_json(), encoding="utf-8")
    print(f"wrote {OPENAPI_FILE}", file=sys.stderr)


def run() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run("heatbeat.api.main:create_app", factory=True, host=settings.api_host, port=settings.api_port)
