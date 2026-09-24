FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder
WORKDIR /app
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.7.8
COPY services/admin-metrics/pyproject.toml services/admin-metrics/uv.lock ./
COPY services/admin-metrics/src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY services/admin-metrics/src ./src
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
RUN useradd --create-home --uid 10001 flowweave \
 && chown -R flowweave:flowweave /app
USER flowweave
CMD ["python", "-m", "flowweave_admin_metrics.main"]
