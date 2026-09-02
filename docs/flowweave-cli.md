# FlowWeave CLI

`flowweave` is a zero-login command-line client for the current FlowWeave Platform API. It stores only a platform base URL; it does not create, retain, or transmit credentials.

## Design

The CLI has three layers:

1. `config init` establishes the one platform fact required by the current deployment: its base URL, including any reverse-proxy prefix.
2. `health` and `openapi` provide a safe connectivity check and live contract discovery.
3. `api` is the complete platform surface. It sends a method, relative path, JSON body, query values and optional one-command headers to the configured platform. `resource` only shortens common collection paths and never becomes a second source of endpoint schemas.

This means a newly deployed REST endpoint is immediately usable through `api`, while the CLI remains small and aligned with FlowWeave's server-owned OpenAPI contract. Multipart and WebSocket transports are exposed as explicit `upload` and `ws` commands rather than being approximated as JSON commands.

## Install and configure

Install the platform package in a Python 3.12 environment, then configure the platform root once. Preserve a deployment prefix such as `/flowweave` in the configured URL.

```bash
cd services/platform
uv sync
uv run flowweave config init --base-url https://hq-ai.hszq8.com/flowweave
uv run flowweave health --ready
```

The default config file is `~/.config/flowweave/config.toml`. Set `FLOWWEAVE_CONFIG_PATH` to use a project-local or test-specific config file.

## Platform-wide endpoint access

`flowweave api` is the stable, complete surface: it prefixes relative paths with `/api/v1`, so every REST route exposed by the running platform is callable without waiting for a CLI release. It accepts JSON inline or from a file, repeated query values and arbitrary HTTP headers.

```bash
# Discover the live contract first.
uv run flowweave openapi --paths

# Read any API resource.
uv run flowweave api get /flows
uv run flowweave api get /flow-runs -q limit=20

# Send a command. Add Idempotency-Key for a retryable mutating endpoint when required by its contract.
uv run flowweave api post /flows \
  --data-file ./flow.json \
  -H 'Idempotency-Key: create-flow-demo'

# Inspect a request without changing platform state.
uv run flowweave api delete /flows/flow-id --dry-run
```

Use `--raw` only for platform-root routes such as `/health` or `/openapi.json`; use `health` and `openapi` when possible.

## Common resources

`resource` is a small convenience layer for common collection paths. It does not hide API payloads or invent defaults.

```bash
uv run flowweave resource flows list
uv run flowweave resource environments list
uv run flowweave resource capabilities get capability-id
uv run flowweave resource node-assets create --data-file ./node-asset.json
```

For nested routes, websocket endpoints, file uploads, or any route not represented by `resource`, use `flowweave openapi --paths` followed by `flowweave api`.

## File upload and WebSocket routes

Use the generic transports for platform endpoints that are not JSON REST calls:

```bash
# A multipart upload; repeat --form or --file as needed.
uv run flowweave upload post /agent-workspaces/workspace-id/attachments \
  --file file=./brief.pdf

# Subscribe to a WebSocket endpoint. Use Ctrl-C to stop an unbounded stream.
uv run flowweave ws /agent-workspaces/workspace-id/runtime/stream
```

`ws --message-json` sends one JSON message immediately after connection; `--max-messages` makes an event stream bounded for scripts. Both commands retain the configured deployment prefix and never persist headers.

## Safety boundary

The current platform has no end-user login endpoint, so this CLI intentionally has no `auth login` command. The CLI rejects URL credentials and absolute endpoint URLs to keep each invocation scoped to the configured platform. It never persists headers supplied with `-H`; treat those headers as one-command input and avoid placing secrets in shell history.
