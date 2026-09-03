---
name: es-query
description: Query market-system ES/EasySearch/Kibana service, access, and reqId trace logs. Use for targeted log queries; full exception collection is orchestrated by collect-app-exception-logs.
---
# ES Query
Use host-based filtering only: resolve application name through references/app-index.md, then pass --host hostname*. Never substitute appName filtering or scan globally.

## Runtime connection
This governed package contains no credentials. The runtime must provide ES_QUERY_USER and ES_QUERY_PASSWORD and an approved ES endpoint configuration. If missing, fail closed.

## Routing
Use service for exceptions, access for endpoint/key traffic, trace for reqId. For trace, first find candidate reqId under the application host, then replay the reqId in chronological order.

## Exception collection
When invoked by collect-app-exception-logs Phase 1, run ERROR and Exception tracks with --classify and the supplied --exclude-config. The caller records exact closure; do not call a top-N or capped result a closure.
