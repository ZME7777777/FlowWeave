# SkyWalking Query Recipes

This document helps you adapt the skill to your deployed SkyWalking version.

## 1. Find the real GraphQL endpoint

Common patterns:

- `/graphql`
- `/graphql/`
- SkyWalking UI origin plus `/graphql`
- a gateway domain that forwards to OAP GraphQL

Current verified local environment:

- HK UI root: `https://skywalking-saman-hkeq.hszq8.com/`
- HK GraphQL: `https://skywalking-saman-hkeq.hszq8.com/graphql`
- GSC UI root: `https://gsc-skywalking.hszq8.com/`
- GSC GraphQL: `https://gsc-skywalking.hszq8.com/graphql`
- Riyadh UI root: `https://skywalking-riyadh.hszq8.com/`
- Riyadh GraphQL: `https://skywalking-riyadh.hszq8.com/graphql`

If the endpoint is unclear:

1. open the internal SkyWalking UI
2. inspect browser network traffic
3. copy the GraphQL request URL and headers
4. place them into `config.json`

## 2. Confirm the schema

Run:

```bash
node skills/skywalking-query/scripts/sw-query.js introspect --env hk
```

If introspection is disabled, use one of:

- copy the GraphQL payload from SkyWalking UI network traffic
- ask a teammate for a known-good query payload
- search internal docs for SkyWalking GraphQL examples

## 3. Common scenario categories

The exact operation names differ by version, but the scenarios are stable:

- trace lookup by `traceId`
- endpoint list for a service
- service topology or endpoint topology
- slow trace search in a time window
- error trace search in a time window
- service instance inspection
- dependency relationship inspection

## 4. Recommended discovery workflow

For a new environment or a fresh deployment:

1. run `introspect`
2. search returned type names for `Trace`, `Topology`, `Service`, `Endpoint`, `Instance`, `Metric`
3. copy one real query from UI network traffic
4. save it as a local payload file
5. use `sw-query.js graphql @payload.json --env <env>`

## 5. Example payload file shape

This is only a transport example, not a version-locked SkyWalking query:

```json
{
  "query": "query Example($id: ID!) { version }",
  "variables": {
    "id": "replace-me"
  }
}
```

## 6. Dashboard extraction

The current script supports direct extraction from SkyWalking dashboard templates.

Example:

```bash
node skills/skywalking-query/scripts/sw-query.js dashboard \
  --env hk \
  --service hq-interface-hkeq-product \
  --template General-Service \
  --tab Overview \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"
```

Notes:

- the time format for dashboard and MQE queries is `YYYY-MM-DD HHmm`
- `General-Service > Overview` has been verified locally against the HK SkyWalking
- the command reads the template config first, then executes every widget expression in that tab
- returned widget types include `Card`, `Line`, and `TopList`

## 7. Topology and trace commands

Service topology:

```bash
node skills/skywalking-query/scripts/sw-query.js topology \
  --env hk \
  --service hq-interface-hkeq-product \
  --kind service \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"
```

Endpoint topology:

```bash
node skills/skywalking-query/scripts/sw-query.js topology \
  --env hk \
  --service hq-interface-hkeq-product \
  --kind endpoint \
  --endpoint "/hq/queryOptionalMinuteKline" \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"
```

Trace search:

```bash
node skills/skywalking-query/scripts/sw-query.js trace-search \
  --env hk \
  --service hq-interface-hkeq-product \
  --trace-state ERROR \
  --query-order BY_DURATION \
  --page-size 5 \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"
```

Trace detail:

```bash
node skills/skywalking-query/scripts/sw-query.js trace-detail \
  --env hk \
  --trace-id 1775050620020.956639304.42462
```

## 8. Result interpretation

When answering users, prefer:

1. trace or topology facts
2. slowest hop or failed span
3. suspected downstream dependency
4. the next system to inspect with ES or Prometheus

Do not pretend schema certainty if the query was reconstructed from UI traffic rather than versioned docs.
