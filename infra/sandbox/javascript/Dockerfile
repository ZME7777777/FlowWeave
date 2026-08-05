FROM node:22.17.0-alpine3.21
WORKDIR /sandbox
COPY infra/sandbox/javascript/runner.mjs /sandbox/runner.mjs
USER 65534:65534
ENTRYPOINT ["node", "/sandbox/runner.mjs"]
