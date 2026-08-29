FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS node_runtime
FROM docker:29.6.1-cli@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b AS docker_cli

FROM opensandbox/server@sha256:91717d1821ffdc698fb0885aab58173c4100ea137b98650dfb361431c1fe3373
ARG INSTALL_ACP_PROVIDERS=""
USER root
WORKDIR /runtime
COPY --from=node_runtime /usr/local/ /usr/local/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
RUN sed -i \
      -e 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|https://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apt/sources.list.d/debian.sources
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      bash ca-certificates coreutils findutils grep gzip python3.13 python3.13-venv \
      python3-pip sed tar util-linux xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      bash-completion curl default-mysql-client dnsutils file gawk git iproute2 iputils-ping \
      jq less netcat-openbsd openssh-client pkg-config procps psmisc tmux tree unzip wget zip \
 && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      build-essential default-jdk-headless maven \
 && rm -rf /var/lib/apt/lists/*
RUN python3.13 -m pip install --break-system-packages --no-cache-dir uv==0.7.8 \
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
COPY infra/openhands/patch_fork_condenser.py /runtime/patch_fork_condenser.py
RUN /usr/local/bin/python /runtime/fetch_openhands_source.py \
      --lock /runtime/openhands-source.lock.json \
      --destination /opt/openhands-source \
      --provenance /runtime/openhands-source-provenance.json \
      --overlay /runtime/patch_fork_condenser.py
RUN /usr/local/bin/python /runtime/patch_fork_condenser.py /opt/openhands-source
RUN uv pip install --python /runtime/.venv/bin/python --no-deps --reinstall \
      /opt/openhands-source/openhands-sdk \
      /opt/openhands-source/openhands-tools \
      /opt/openhands-source/openhands-workspace \
      /opt/openhands-source/openhands-agent-server
RUN set -eux; \
    packages=""; \
    for provider in $(echo "$INSTALL_ACP_PROVIDERS" | tr ',' ' '); do \
      case "$provider" in \
        claude-code) \
          packages="$packages @agentclientprotocol/claude-agent-acp@0.63.0";; \
        codex) \
          packages="$packages @agentclientprotocol/codex-acp@1.1.7";; \
        gemini-cli) \
          packages="$packages @google/gemini-cli@0.46.0";; \
        *) \
          echo "Unknown ACP provider '$provider' in INSTALL_ACP_PROVIDERS" >&2; \
          exit 1;; \
      esac; \
    done; \
    if [ -n "$packages" ]; then \
      npm install --global $packages; \
      npm cache clean --force; \
    else \
      echo "INSTALL_ACP_PROVIDERS is empty; no ACP providers will be installed"; \
    fi
RUN /runtime/.venv/bin/python -c \
      "from importlib.metadata import version; expected='1.44.0'; packages=('openhands-agent-server','openhands-sdk','openhands-tools','openhands-workspace'); actual={name: version(name) for name in packages}; assert all(value == expected for value in actual.values()), actual"
COPY infra/openhands/contract_check.py /runtime/contract_check.py
COPY infra/openhands/contract_check.py \
     /opt/openhands-source/openhands-agent-server/openhands/agent_server/flowweave_contract_check.py
COPY infra/openhands/plugin_resolver.py /runtime/plugin_resolver.py
ENV PATH="/runtime/.venv/bin:/usr/local/bin:$PATH"
ENV OPENHANDS_BUILD_GIT_SHA="9a24f6c8866f353042a57df0514ccc900e3a0691" \
    OPENHANDS_BUILD_GIT_REF="9a24f6c8866f353042a57df0514ccc900e3a0691"
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
