FROM opensandbox/server:latest
USER root
WORKDIR /runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3.13 python3.13-venv python3-pip git ca-certificates tmux \
 && rm -rf /var/lib/apt/lists/* \
 && python3.13 -m pip install --break-system-packages --no-cache-dir uv==0.7.8
COPY infra/openhands/pyproject.toml infra/openhands/uv.lock ./
ENV UV_PROJECT_ENV=/runtime/.venv
RUN uv sync --frozen --no-dev
ENV PATH="/runtime/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT []
CMD ["agent-server", "--host", "0.0.0.0", "--port", "8000"]
