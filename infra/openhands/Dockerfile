FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS node_runtime
FROM python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS python_runtime
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv_runtime
FROM docker:29.6.1-cli@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b AS docker_cli

# Fixed baseline's pip re-vendoring: patched urllib3/msgpack, no pkg_resources.
# Keep source checksum and vendor edits aligned with the archived SDK Dockerfile.
FROM python:3.13.17-trixie@sha256:d5a186865324088bd10d1671801b42297a8fe3f8059143fb22b2e2b776ffa6dd AS pip-revendored
ADD --checksum=sha256:b505949bb27827b32f54c9f1f5ca660a6fae3fde61cbe0a971975a3ed34a7ee1 \
    https://github.com/pypa/pip/archive/refs/tags/26.2.2.tar.gz /tmp/pip.tar.gz
RUN set -eux; \
    mkdir /tmp/pip; \
    tar -xzf /tmp/pip.tar.gz -C /tmp/pip --strip-components=1; \
    cd /tmp/pip; \
    sed -i -E \
        -e 's/^( +)urllib3==2\.7\.0$/\1urllib3==2.8.0/' \
        -e 's/^msgpack==1\.1\.2$/msgpack==1.2.1/' \
        -e '/^setuptools==70\.3\.0$/d' \
        src/pip/_vendor/vendor.txt; \
    grep -qx '    urllib3==2.8.0' src/pip/_vendor/vendor.txt; \
    grep -qx 'msgpack==1.2.1' src/pip/_vendor/vendor.txt; \
    if grep -q '^setuptools==' src/pip/_vendor/vendor.txt; then exit 1; fi; \
    rm tools/vendoring/patches/pkg_resources.patch; \
    python -m venv /tmp/vendoring; \
    /tmp/vendoring/bin/pip install --no-cache-dir vendoring==1.4.0; \
    /tmp/vendoring/bin/vendoring sync; \
    python -m pip wheel --no-deps --no-cache-dir --wheel-dir /dist .


FROM opensandbox/server@sha256:91717d1821ffdc698fb0885aab58173c4100ea137b98650dfb361431c1fe3373
ARG INSTALL_ACP_PROVIDERS=""
USER root
WORKDIR /runtime
# Dependency downloads are disposable; do not bake them into image layers or
# copy them into each Environment HOME when the Runtime is first provisioned.
ENV UV_NO_CACHE=1 PIP_NO_CACHE_DIR=1
COPY --from=node_runtime /usr/local/ /usr/local/
COPY --from=python_runtime /usr/local/ /usr/local/
COPY --from=uv_runtime /uv /uvx /usr/local/bin/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
# Match the adopted upstream CA-trust boundary without using rolling apt inputs.
ARG DEBIAN_SNAPSHOT=20261021T000000Z
RUN sed -i \
      -e "s|https\?://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/|g" \
      -e "s|https\?://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/|g" \
      /etc/apt/sources.list.d/debian.sources \
 && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 upgrade -y \
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
      build-essential default-jdk-headless maven vim \
 && rm -rf /var/lib/apt/lists/*
# The fixed Agent Workspace seed and every Environment setup session start
# from this image. Keep Chromium in that common baseline so browser-enabled
# OpenHands conversations never depend on a session-local package install.
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/*
ENV CHROME_BIN=/usr/bin/chromium \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"
COPY --from=pip-revendored /dist/ /runtime/pip-dist/
RUN /usr/local/bin/python3.13 -m pip install --no-deps --no-index --force-reinstall \
      --disable-pip-version-check --root-user-action=ignore /runtime/pip-dist/pip-*.whl \
 && npm install --global npm@11.21.0 @larksuite/cli@1.0.84 \
 && npm cache clean --force \
 && ln -sf /usr/local/bin/python3.13 /usr/local/bin/python \
 && ln -sf /usr/local/bin/python3.13 /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/node /usr/local/bin/js
COPY infra/openhands/pyproject.toml infra/openhands/uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/runtime/.venv
RUN uv sync --frozen --no-dev --group source-build
COPY infra/openhands/source.lock.json /runtime/openhands-source.lock.json
COPY infra/openhands/vendor/openhands-source-ecf0aec091463fb242b0c5ab59d155c65d96ded8.tar.gz /runtime/openhands-source-ecf0aec091463fb242b0c5ab59d155c65d96ded8.tar.gz
COPY infra/openhands/fetch_source.py /runtime/fetch_openhands_source.py
RUN /usr/local/bin/python /runtime/fetch_openhands_source.py \
      --lock /runtime/openhands-source.lock.json \
      --destination /opt/openhands-source \
      --provenance /runtime/openhands-source-provenance.json
RUN uv pip install --python /runtime/.venv/bin/python --no-build-isolation --no-deps --reinstall \
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
      "from importlib.metadata import version; expected='1.58.2'; packages=('openhands-agent-server','openhands-sdk','openhands-tools','openhands-workspace'); actual={name: version(name) for name in packages}; assert all(value == expected for value in actual.values()), actual"
COPY infra/openhands/contract_check.py /runtime/contract_check.py
COPY infra/openhands/contract_check.py \
     /opt/openhands-source/openhands-agent-server/openhands/agent_server/flowweave_contract_check.py
COPY infra/openhands/plugin_resolver.py /runtime/plugin_resolver.py
ENV PATH="/runtime/.venv/bin:/usr/local/bin:$PATH"
ENV OPENHANDS_BUILD_GIT_SHA="ecf0aec091463fb242b0c5ab59d155c65d96ded8" \
    OPENHANDS_BUILD_GIT_REF="ecf0aec091463fb242b0c5ab59d155c65d96ded8"
RUN /runtime/.venv/bin/python /runtime/contract_check.py
RUN uv pip install --python /runtime/.venv/bin/python --no-deps /runtime/pip-dist/pip-*.whl \
 && ln -sf /runtime/.venv/bin/pip /usr/local/bin/pip \
 && ln -sf /runtime/.venv/bin/pip /usr/local/bin/pip3
RUN set -eu; \
    for command in bash node npm npx python python3 pip pip3 uv java javac mvn mysql \
      git ssh curl wget jq zip unzip tar gzip make gcc g++ sed awk grep find xargs ps top \
      ip ping nslookup nc tmux vim chromium agent-server; do \
      command -v "$command" >/dev/null; \
    done
RUN apt-get purge -y python3-pip
COPY infra/openhands/image_security_check.py /runtime/image_security_check.py
RUN /runtime/.venv/bin/python /runtime/image_security_check.py \
 && test "$(node --version)" = "v24.22.1" \
 && test "$(npm --version)" = "11.21.0"
RUN /runtime/.venv/bin/python -c \
    "from openhands.tools.browser_use import BrowserToolSet; from openhands.tools.browser_use.impl import BrowserToolExecutor; assert BrowserToolSet.name == 'browser_tool_set'; assert BrowserToolExecutor.check_chromium_available() == '/usr/bin/chromium'"
EXPOSE 8000
ENTRYPOINT []
CMD ["agent-server", "--host", "0.0.0.0", "--port", "8000"]
