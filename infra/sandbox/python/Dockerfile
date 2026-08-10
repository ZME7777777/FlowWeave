FROM python:3.12.11-alpine3.21@sha256:aed27d7387bcf51a8f43b4a5b998857591c7933caa405920267ad42fb609a6d5
WORKDIR /sandbox
COPY infra/sandbox/python/runner.py /sandbox/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-S", "/sandbox/runner.py"]
