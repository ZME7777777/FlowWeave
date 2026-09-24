FROM node:22.17.0-alpine3.22@sha256:fc3e945f920b7e3000cd1af86c4ae406ec70c72f328b667baf0f3a8910d69eed AS build
ARG VITE_ADMIN_BASE_PATH=/flowweave/admin/
ARG VITE_ADMIN_API_PROXY_TARGET
ENV VITE_ADMIN_BASE_PATH=${VITE_ADMIN_BASE_PATH} VITE_ADMIN_API_PROXY_TARGET=${VITE_ADMIN_API_PROXY_TARGET}
WORKDIR /workspace
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/admin-web/package.json apps/admin-web/package.json
RUN pnpm install --frozen-lockfile
COPY apps/admin-web apps/admin-web
RUN pnpm --filter @flowweave/admin-web build

FROM nginx:1.28.0-alpine3.21@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
COPY --from=build /workspace/apps/admin-web/dist /usr/share/nginx/html
COPY apps/admin-web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
