FROM python:3.12.11-alpine3.21@sha256:aed27d7387bcf51a8f43b4a5b998857591c7933caa405920267ad42fb609a6d5
WORKDIR /sandbox
RUN sed -i \
      -e 's|http://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|https://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apk/repositories \
 && apk add --no-cache vim
COPY infra/sandbox/python/runner.py /sandbox/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-S", "/sandbox/runner.py"]
