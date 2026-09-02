---
name: flowweave
description: Operate a FlowWeave Platform deployment through its configured `flowweave` CLI, including Flow, run, environment, capability, and Agent Workspace REST operations. Use for FlowWeave platform administration or automation; not for direct OpenHands or Docker control.
---

# FlowWeave CLI

Use the `flowweave` CLI as the entry point for FlowWeave REST work. The current platform does not require CLI login; configure only the platform base URL, preserving any deployment prefix:

```bash
flowweave config init --base-url https://host.example/flowweave
flowweave health --ready
```

## Discover and call the contract

Treat the live OpenAPI document as the authority for available REST paths and request bodies:

```bash
flowweave openapi --paths
flowweave api get /flows
```

`flowweave api` automatically adds `/api/v1` to relative resource paths. Use `--raw` only for platform-root endpoints. Pass JSON bodies with `--data` or `--data-file`; use `-H 'Idempotency-Key: …'` when a mutating endpoint requires retry-safe command identity.

For common top-level resources, `flowweave resource <flows|runs|environments|capabilities|node-assets|node-directories|model-providers|memory-sources|capability-collections> <list|get|create|update|delete>` provides a concise path mapping. Use `api` for nested routes and any newer JSON endpoint; use `upload` for multipart routes and `ws` for WebSocket streams.

## Boundaries

- Keep actions within the user-requested FlowWeave resource scope; read live state before mutation when target identity matters.
- `--dry-run` verifies the final method, URL and JSON body without changing platform state.
- The CLI does not save credentials or arbitrary headers. Do not put secrets in config files, skill instructions, committed scripts, or shell history.
- Do not bypass FlowWeave with Docker, a Runtime Provider endpoint, direct database access, or OpenHands private APIs. FlowWeave remains the governance control plane.
