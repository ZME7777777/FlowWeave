# FlowWeave versioned contracts

All cross-process JSON structures use JSON Schema 2020-12 and an explicit `schema_version` where applicable.

- `openapi-v1.json`: frozen public HTTP API baseline. Contract tests compare the generated FastAPI document structurally.
- `run-event.schema.json`: persisted/SSE run event record returned by `/api/v1/flow-runs/{id}/event-history`.
- `runtime-result.schema.json`: normalized Runtime adapter result consumed by orchestration.
- `gate-input.schema.json`: normalized input passed to a Gate/Sandbox execution.
- `gate-result.schema.json`: normalized Gate/Sandbox result.
- `review-package.schema.json`: review package used by agent packages.

Adding optional fields is compatible. Removing or renaming fields, changing types, or changing semantics requires an explicit contract version and reviewed baseline update.
