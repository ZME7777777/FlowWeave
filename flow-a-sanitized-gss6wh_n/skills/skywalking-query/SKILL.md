---
name: skywalking-query
description: Query SkyWalking trace details, topology, endpoint evidence, and error traces for Phase 2 exception investigation or explicit trace analysis.
---
# SkyWalking Query
This governed package contains no connection credentials. Use only protected runtime configuration or protected runtime inputs; fail closed when unavailable. For each fingerprint, prefer known traceId; otherwise search ERROR then ALL by observed service, endpoint, and time. Record unavailable sources and do not use SG as HK substitute.
