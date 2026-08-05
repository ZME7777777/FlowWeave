FROM python:3.12.11-alpine3.21
WORKDIR /sandbox
COPY infra/sandbox/python/runner.py /sandbox/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-S", "/sandbox/runner.py"]
