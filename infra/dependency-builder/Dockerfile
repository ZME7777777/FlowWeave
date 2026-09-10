FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS node_runtime

FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419
WORKDIR /builder
RUN sed -i \
      -e 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|https://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends vim \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/ /usr/local/
COPY infra/dependency-builder/runner.py /builder/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "/builder/runner.py"]
