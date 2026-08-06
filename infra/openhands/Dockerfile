FROM node:22.18.0-bookworm-slim AS node_runtime

FROM opensandbox/server:latest
USER root
WORKDIR /runtime
COPY --from=node_runtime /usr/local/ /usr/local/
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl jq unzip openssh-client python3.13 python3.13-venv python3-pip git ca-certificates tmux \
 && rm -rf /var/lib/apt/lists/* \
 && python3.13 -m pip install --break-system-packages --no-cache-dir uv==0.7.8 \
 && npm install --global @larksuite/cli@1.0.84 \
 && npm cache clean --force \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/node /usr/local/bin/js
COPY infra/openhands/pyproject.toml infra/openhands/uv.lock ./
ENV UV_PROJECT_ENV=/runtime/.venv
RUN uv sync --frozen --no-dev
ENV PATH="/runtime/.venv/bin:/usr/local/bin:$PATH"
EXPOSE 8000
ENTRYPOINT []
CMD ["agent-server", "--host", "0.0.0.0", "--port", "8000"]
