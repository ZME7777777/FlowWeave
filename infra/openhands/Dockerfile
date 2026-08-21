FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS node_runtime
FROM docker:29.6.1-cli@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b AS docker_cli

FROM opensandbox/server@sha256:91717d1821ffdc698fb0885aab58173c4100ea137b98650dfb361431c1fe3373
USER root
WORKDIR /runtime
COPY --from=node_runtime /usr/local/ /usr/local/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
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
COPY infra/openhands/source.lock.json /runtime/openhands-source.lock.json
COPY infra/openhands/fetch_source.py /runtime/fetch_openhands_source.py
COPY infra/openhands/patch_ambient_plugins.py /runtime/patch_ambient_plugins.py
RUN /usr/local/bin/python /runtime/fetch_openhands_source.py \
      --lock /runtime/openhands-source.lock.json \
      --destination /opt/openhands-source \
      --provenance /runtime/openhands-source-provenance.json \
      --overlay /runtime/patch_ambient_plugins.py
RUN /usr/local/bin/python /runtime/patch_ambient_plugins.py \
      --site-packages /opt/openhands-source \
 && cp /runtime/openhands-source-provenance.json \
      /opt/openhands-source/openhands-agent-server/openhands/agent_server/openhands-source-provenance.json
RUN uv pip install --python /runtime/.venv/bin/python --no-deps --reinstall \
      /opt/openhands-source/openhands-sdk \
      /opt/openhands-source/openhands-tools \
      /opt/openhands-source/openhands-workspace \
      /opt/openhands-source/openhands-agent-server
RUN /runtime/.venv/bin/python -c \
      "from importlib.metadata import version; expected='1.42.0'; packages=('openhands-agent-server','openhands-sdk','openhands-tools','openhands-workspace'); actual={name: version(name) for name in packages}; assert all(value == expected for value in actual.values()), actual"
RUN /runtime/.venv/bin/python /runtime/patch_ambient_plugins.py
COPY infra/openhands/contract_check.py /runtime/contract_check.py
COPY infra/openhands/contract_check.py \
     /opt/openhands-source/openhands-agent-server/openhands/agent_server/flowweave_contract_check.py
COPY infra/openhands/plugin_resolver.py /runtime/plugin_resolver.py
ENV PATH="/runtime/.venv/bin:/usr/local/bin:$PATH"
ENV OPENHANDS_BUILD_GIT_SHA="f09e03eac772290feeb51b7d7390ffaefeca1a09" \
    OPENHANDS_BUILD_GIT_REF="f09e03eac772290feeb51b7d7390ffaefeca1a09"
RUN /runtime/.venv/bin/python /runtime/contract_check.py
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
