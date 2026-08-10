FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS node_runtime

FROM opensandbox/server@sha256:91717d1821ffdc698fb0885aab58173c4100ea137b98650dfb361431c1fe3373
USER root
WORKDIR /runtime
COPY --from=node_runtime /usr/local/ /usr/local/
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      bash bash-completion build-essential ca-certificates coreutils curl default-jdk-headless \
      default-mysql-client dnsutils file findutils gawk git grep gzip iproute2 iputils-ping jq \
      less maven netcat-openbsd openssh-client pkg-config procps psmisc python3.13 \
      python3.13-venv python3-pip sed tar tmux tree unzip util-linux wget xz-utils zip \
 && rm -rf /var/lib/apt/lists/* \
 && python3.13 -m pip install --break-system-packages --no-cache-dir uv==0.7.8 \
 && npm install --global @larksuite/cli@1.0.84 \
 && npm cache clean --force \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python3 \
 && ln -sf /usr/bin/pip3 /usr/local/bin/pip \
 && ln -sf /usr/bin/pip3 /usr/local/bin/pip3 \
 && ln -sf /usr/local/bin/node /usr/local/bin/js
COPY infra/openhands/pyproject.toml infra/openhands/uv.lock ./
ENV UV_PROJECT_ENV=/runtime/.venv
RUN uv sync --frozen --no-dev
ENV PATH="/runtime/.venv/bin:/usr/local/bin:$PATH"
RUN uv pip install --python /runtime/.venv/bin/python pip==25.1.1 \
 && ln -sf /runtime/.venv/bin/pip /usr/local/bin/pip \
 && ln -sf /runtime/.venv/bin/pip /usr/local/bin/pip3
RUN set -eu; \
    for command in bash node npm npx python python3 pip pip3 uv java javac mvn mysql \
      git ssh curl wget jq zip unzip tar gzip make gcc g++ sed awk grep find xargs ps top \
      ip ping nslookup nc tmux agent-server; do \
      command -v "$command" >/dev/null; \
    done
EXPOSE 8000
ENTRYPOINT []
CMD ["agent-server", "--host", "0.0.0.0", "--port", "8000"]
