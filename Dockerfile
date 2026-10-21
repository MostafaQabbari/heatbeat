# What this file does: builds one image used for the bridge, the API, the
# simulator and the migrations (docker-compose.yml picks the command).

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first: this layer is cached until uv.lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Do not run as root inside the container.
RUN useradd --create-home --uid 10001 app
USER app

CMD ["heatbeat-api"]
