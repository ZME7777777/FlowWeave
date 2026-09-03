---
name: es-query
description: Query market-system ES service, access, and reqId trace logs. Use for targeted logs; full exception collection is orchestrated by collect-app-exception-logs.
---
# ES Query
Resolve application name via references/app-index.md and query only with --host hostname*.

## Runtime connection
This governed package has no credentials. Runtime must inject ES_QUERY_USER and ES_QUERY_PASSWORD plus approved endpoint settings. Missing configuration fails closed.

## Phase 1
For exception collection, execute ERROR and Exception tracks with --classify and supplied --exclude-config. Exact count closure remains mandatory.
