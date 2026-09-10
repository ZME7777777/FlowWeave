FROM node:22.17.0-alpine3.21@sha256:0d722d537f07d5e962b82733cd641cfa1fb868eab8c597ebfc87b8fa0436daa9
WORKDIR /sandbox
RUN sed -i \
      -e 's|http://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|https://dl-cdn.alpinelinux.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apk/repositories \
 && apk add --no-cache vim
COPY infra/sandbox/javascript/runner.mjs /sandbox/runner.mjs
USER 65534:65534
ENTRYPOINT ["node", "/sandbox/runner.mjs"]
