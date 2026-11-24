# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY python/pyproject.toml python/uv.lock ./
COPY contracts ./contracts
COPY python/packages ./packages
COPY python/services ./services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-dev \
    && useradd --create-home --uid 10001 wre
USER 10001:10001

ENTRYPOINT ["python", "-m"]
