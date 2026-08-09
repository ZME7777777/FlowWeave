FROM node:22.18.0-bookworm-slim AS node_runtime

FROM python:3.13.5-slim-bookworm
WORKDIR /builder
COPY --from=node_runtime /usr/local/ /usr/local/
COPY infra/dependency-builder/runner.py /builder/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "/builder/runner.py"]
