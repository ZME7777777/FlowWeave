---
name: skywalking-query
description: Query SkyWalking trace details, topology, endpoint evidence, and error traces. Use for Phase 2 exception investigation or explicit trace analysis.
---
# SkyWalking Query
This governed package contains no connection credentials. Provide endpoint and authorization only through approved runtime configuration or explicit protected runtime inputs; fail closed if unavailable.

For a fingerprint, use the observed application, endpoint, and time window. Prefer a known traceId; otherwise search ERROR then ALL traces and inspect selected trace details. Record evidence, unavailable sources, and uncertainty. Do not use SG evidence as an HK substitute.
