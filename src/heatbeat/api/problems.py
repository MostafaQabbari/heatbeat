"""Errors as application/problem+json (RFC 9457).

What this file does: one exception type for routes (`ApiProblem`) and the
handlers that turn every error, including FastAPI's own validation errors,
into the same JSON shape with the right status code and headers.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from heatbeat.api.schemas import Problem

PROBLEM_JSON = "application/problem+json"


class ApiProblem(Exception):
    def __init__(
        self, status: int, title: str, detail: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.problem = Problem(status=status, title=title, detail=detail)
        self.headers = headers or {}


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(problem.model_dump(), status_code=problem.status, media_type=PROBLEM_JSON, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def on_api_problem(_req: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(exc.problem, exc.headers)

    @app.exception_handler(HTTPException)
    async def on_http_error(_req: Request, exc: HTTPException) -> JSONResponse:
        headers = dict(exc.headers) if exc.headers else None
        return problem_response(Problem(status=exc.status_code, title=str(exc.detail)), headers)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_req: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", []))
        return problem_response(Problem(status=422, title="Invalid request", detail=f"{where}: {first['msg']}"))
