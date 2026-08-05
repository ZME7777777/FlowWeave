FROM docker:29.6.1-cli@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b AS docker_cli

FROM python:3.12.11-slim-bookworm AS builder
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.7.8
COPY services/platform/pyproject.toml services/platform/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12.11-slim-bookworm AS runtime
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.venv /app/.venv
COPY --from=docker_cli /usr/local/bin/docker /usr/bin/docker
COPY services/platform/alembic.ini ./alembic.ini
COPY services/platform/migrations ./migrations
COPY services/platform/src ./src
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
RUN useradd --create-home --uid 10001 flowweave \
 && mkdir -p /data/artifacts /data/workspaces \
 && chown -R flowweave:flowweave /app /data
USER flowweave
EXPOSE 8080
CMD ["uvicorn", "flowweave.bootstrap.api:create_app", "--host", "0.0.0.0", "--port", "8080", "--factory"]
