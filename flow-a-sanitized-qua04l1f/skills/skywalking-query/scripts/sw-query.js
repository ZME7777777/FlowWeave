#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const CONFIG_PATH = path.join(ROOT, "config.json");
const TEMPLATE_CONFIG_PATH = path.join(ROOT, "config.template.json");

function printUsage() {
  console.log(`Usage:
  node skills/skywalking-query/scripts/sw-query.js examples
  node skills/skywalking-query/scripts/sw-query.js scenarios
  node skills/skywalking-query/scripts/sw-query.js auto --service <service> [--query "<text>"] [--intent <kind>] [--trace-id <id>] [--endpoint <keyword>] [--start "YYYY-MM-DD HHmm"] [--end "YYYY-MM-DD HHmm"] [--env <name>]
  node skills/skywalking-query/scripts/sw-query.js introspect --env <name>
  node skills/skywalking-query/scripts/sw-query.js graphql <json|@file> [--env <name>] [--url <graphql-url>] [--header "Key: Value"]
  node skills/skywalking-query/scripts/sw-query.js template --template <template-id> [--env <name>]
  node skills/skywalking-query/scripts/sw-query.js dashboard --service <service-name> [--template General-Service] [--tab Overview] [--start "YYYY-MM-DD HHmm"] [--end "YYYY-MM-DD HHmm"] [--scope Service"] [--env <name>]
  node skills/skywalking-query/scripts/sw-query.js topology --service <service-name> [--kind service|endpoint] [--endpoint <keyword>] [--start "YYYY-MM-DD HHmm"] [--end "YYYY-MM-DD HHmm"] [--env <name>]
  node skills/skywalking-query/scripts/sw-query.js trace-search --service <service-name> [--endpoint <keyword>] [--trace-state ERROR|SUCCESS|ALL] [--query-order BY_DURATION|BY_START_TIME] [--min-duration <ms>] [--max-duration <ms>] [--page-size <n>] [--start "YYYY-MM-DD HHmm"] [--end "YYYY-MM-DD HHmm"] [--env <name>]
  node skills/skywalking-query/scripts/sw-query.js trace-detail --trace-id <id> [--env <name>]

Commands:
  examples     Print example payloads and common usage
  scenarios    Print which command to use for each common scenario
  introspect   Run GraphQL introspection against configured or explicit endpoint
  graphql      Send a GraphQL request payload
  template     Fetch a SkyWalking dashboard template definition
  dashboard    Execute all widget expressions in a dashboard tab
  auto         Route to the best query based on scenario text or explicit intent
  topology     Query service or endpoint topology
  trace-search Search traces by service or endpoint in a time range
  trace-detail Query a full trace by traceId

Options:
  --env <name>       Environment name from config.json
  --url <url>        GraphQL URL, overrides config
  --header <kv>      Extra HTTP header, repeatable, format: "Key: Value"
  --service <name>   Service name for dashboard queries
  --app <name>       Alias of --service
  --template <id>    Dashboard template id, default: General-Service
  --tab <name>       Dashboard tab name, default: Overview
  --start <time>     Time range start, format: YYYY-MM-DD HHmm
  --end <time>       Time range end, format: YYYY-MM-DD HHmm
  --scope <scope>    Entity scope, default: Service
  --normal <bool>    Entity normal flag, default: true
  --endpoint <name>  Endpoint keyword or exact endpoint name
  --kind <type>      Topology kind: service or endpoint
  --trace-id <id>    Trace id for trace-detail
  --trace-state <s>  Trace state: ALL, SUCCESS, ERROR
  --query-order <o>  Trace order: BY_START_TIME, BY_DURATION
  --min-duration <n> Minimum trace duration in ms
  --max-duration <n> Maximum trace duration in ms
  --page-size <n>    Trace search page size, default: 20
  --intent <kind>    overview, instance, endpoint, topology, endpoint-topology, trace-search, trace-detail
  --query <text>     Natural-language scenario hint for auto routing
  --raw              Print compact JSON
`);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function resolveConfig() {
  if (fs.existsSync(CONFIG_PATH)) {
    return readJson(CONFIG_PATH);
  }
  if (fs.existsSync(TEMPLATE_CONFIG_PATH)) {
    return readJson(TEMPLATE_CONFIG_PATH);
  }
  throw new Error("No config.json or config.template.json found.");
}

function parseArgs(argv) {
  const args = { _: [], headers: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      args._.push(token);
      continue;
    }
    if (token === "--env") {
      args.env = argv[++i];
      continue;
    }
    if (token === "--url") {
      args.url = argv[++i];
      continue;
    }
    if (token === "--header") {
      args.headers.push(argv[++i]);
      continue;
    }
    if (token === "--raw") {
      args.raw = true;
      continue;
    }
    if (token === "--service") {
      args.service = argv[++i];
      continue;
    }
    if (token === "--app") {
      args.service = argv[++i];
      continue;
    }
    if (token === "--template") {
      args.template = argv[++i];
      continue;
    }
    if (token === "--tab") {
      args.tab = argv[++i];
      continue;
    }
    if (token === "--start") {
      args.start = argv[++i];
      continue;
    }
    if (token === "--end") {
      args.end = argv[++i];
      continue;
    }
    if (token === "--scope") {
      args.scope = argv[++i];
      continue;
    }
    if (token === "--normal") {
      args.normal = argv[++i];
      continue;
    }
    if (token === "--endpoint") {
      args.endpoint = argv[++i];
      continue;
    }
    if (token === "--kind") {
      args.kind = argv[++i];
      continue;
    }
    if (token === "--trace-id") {
      args.traceId = argv[++i];
      continue;
    }
    if (token === "--trace-state") {
      args.traceState = argv[++i];
      continue;
    }
    if (token === "--query-order") {
      args.queryOrder = argv[++i];
      continue;
    }
    if (token === "--min-duration") {
      args.minDuration = argv[++i];
      continue;
    }
    if (token === "--max-duration") {
      args.maxDuration = argv[++i];
      continue;
    }
    if (token === "--page-size") {
      args.pageSize = argv[++i];
      continue;
    }
    if (token === "--intent") {
      args.intent = argv[++i];
      continue;
    }
    if (token === "--query") {
      args.query = argv[++i];
      continue;
    }
    throw new Error(`Unknown option: ${token}`);
  }
  return args;
}

function parseHeaderPairs(headerPairs) {
  const headers = {};
  for (const pair of headerPairs || []) {
    const idx = pair.indexOf(":");
    if (idx <= 0) {
      throw new Error(`Invalid header format: ${pair}`);
    }
    const key = pair.slice(0, idx).trim();
    const value = pair.slice(idx + 1).trim();
    headers[key] = value;
  }
  return headers;
}

function resolveEndpoint(config, cliArgs) {
  const environments = config.environments || {};
  const envName = cliArgs.env || config.default_env;
  const envConfig = envName ? environments[envName] : null;
  const url = cliArgs.url || (envConfig && envConfig.url);
  if (!url) {
    throw new Error("No GraphQL URL found. Use --url or define it in config.json.");
  }

  return {
    envName,
    url,
    headers: {
      "content-type": "application/json",
      ...(config.common_headers || {}),
      ...((envConfig && envConfig.headers) || {}),
      ...parseHeaderPairs(cliArgs.headers),
    },
  };
}

function readPayload(input) {
  if (!input) {
    throw new Error("Missing GraphQL payload. Pass inline JSON or @file.");
  }
  if (input.startsWith("@")) {
    return readJson(path.resolve(process.cwd(), input.slice(1)));
  }
  return JSON.parse(input);
}

async function postGraphql(url, headers, payload) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });

    const text = await res.text();
    let json;
    try {
      json = JSON.parse(text);
    } catch (error) {
      if ((res.status === 503 || res.status === 502) && attempt < 3) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 500));
        continue;
      }
      throw new Error(`Non-JSON response (${res.status}): ${text}`);
    }

    if (!res.ok) {
      if ((res.status === 503 || res.status === 502) && attempt < 3) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 500));
        continue;
      }
      throw new Error(`HTTP ${res.status}: ${JSON.stringify(json)}`);
    }
    return json;
  }
  throw new Error("GraphQL request failed after retries.");
}

function printJson(value, raw) {
  console.log(raw ? JSON.stringify(value) : JSON.stringify(value, null, 2));
}

function printExamples() {
  const examples = {
    introspection: {
      command: "node skills/skywalking-query/scripts/sw-query.js introspect --env hk",
    },
    trace_lookup: {
      note: "Replace the query body with the one confirmed for your SkyWalking version.",
      command:
        'node skills/skywalking-query/scripts/sw-query.js graphql @payload.trace.json --env hk',
      payload_file_example: {
        query: "query Example($traceId: ID!) { version }",
        variables: {
          traceId: "replace-me",
        },
      },
    },
    explicit_url: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js graphql \'{"query":"query { version }"}\' --url http://skywalking.example.com/graphql --header "Authorization: Bearer token"',
    },
    dashboard_overview: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js dashboard --env hk --service hq-interface-hkeq-product --template General-Service --tab Overview --start "2026-04-01 2112" --end "2026-04-01 2142"',
      note: "Runs every widget expression in the selected dashboard tab and returns cards, time-series, and top-list data.",
    },
    topology_service: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js topology --env hk --service hq-interface-hkeq-product --kind service --start "2026-04-01 2112" --end "2026-04-01 2142"',
    },
    trace_search_error: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js trace-search --env hk --service hq-interface-hkeq-product --trace-state ERROR --query-order BY_DURATION --page-size 20 --start "2026-04-01 2112" --end "2026-04-01 2142"',
    },
    trace_detail: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js trace-detail --env hk --trace-id replace-me',
    },
    auto: {
      command:
        'node skills/skywalking-query/scripts/sw-query.js auto --env hk --service hq-interface-hkeq-product --query "查 overview 指标"',
    },
  };
  printJson(examples, false);
}

function printScenarios(raw) {
  const scenarios = [
    {
      scene: "Overview 页指标总览",
      use: "dashboard",
      args: "--template General-Service --tab Overview --service <service>",
    },
    {
      scene: "Instance 页实例列表和明细指标",
      use: "dashboard",
      args: "--template General-Service --tab Instance --service <service>",
    },
    {
      scene: "Endpoint 页接口列表和明细指标",
      use: "dashboard",
      args: "--template General-Service --tab Endpoint --service <service>",
    },
    {
      scene: "看服务调用拓扑 / 下游依赖",
      use: "topology",
      args: "--kind service --service <service>",
    },
    {
      scene: "看某个 endpoint 依赖拓扑",
      use: "topology",
      args: "--kind endpoint --service <service> --endpoint <endpoint keyword>",
    },
    {
      scene: "查某服务最近慢请求 / 错误 trace",
      use: "trace-search",
      args: "--service <service> --trace-state ERROR|ALL --query-order BY_DURATION",
    },
    {
      scene: "按 traceId 查完整调用链",
      use: "trace-detail",
      args: "--trace-id <traceId>",
    },
  ];
  printJson(scenarios, raw);
}

function average(numbers) {
  if (!numbers.length) {
    return null;
  }
  return numbers.reduce((sum, n) => sum + n, 0) / numbers.length;
}

function parseNumericValues(series) {
  return (series.values || [])
    .map((item) => Number(item.value))
    .filter((item) => Number.isFinite(item));
}

function summarizeDashboardWidget(widget) {
  const expressionSummaries = widget.expressions.map((item) => {
    const series = item.series || [];
    if (item.resultType === "SINGLE_VALUE") {
      const value = series[0]?.values?.[0]?.value ?? null;
      return {
        expression: item.expression,
        resultType: item.resultType,
        value,
      };
    }
    if (item.resultType === "TIME_SERIES_VALUES") {
      const values = parseNumericValues(series[0] || { values: [] });
      return {
        expression: item.expression,
        resultType: item.resultType,
        points: values.length,
        min: values.length ? Math.min(...values) : null,
        max: values.length ? Math.max(...values) : null,
        avg: values.length ? Number(average(values).toFixed(3)) : null,
        latest: values.length ? values[values.length - 1] : null,
      };
    }
    if (item.resultType === "SORTED_LIST") {
      const values = (series[0]?.values || []).slice(0, 5).map((entry) => ({
        name:
          entry.owner?.endpointName ||
          entry.owner?.serviceInstanceName ||
          entry.owner?.serviceName ||
          entry.id,
        value: entry.value,
      }));
      return {
        expression: item.expression,
        resultType: item.resultType,
        top: values,
      };
    }
    return {
      expression: item.expression,
      resultType: item.resultType,
      seriesCount: series.length,
    };
  });

  return {
    title: widget.title,
    widgetType: widget.widgetType,
    expressions: expressionSummaries,
  };
}

function summarizeDashboard(widgetResults) {
  return widgetResults.map(summarizeDashboardWidget);
}

function summarizeTopology(topology) {
  const nodeCount = topology?.nodes?.length || 0;
  const edgeCount = topology?.calls?.length || 0;
  const sampleNodes = (topology?.nodes || []).slice(0, 10).map((node) => ({
    id: node.id,
    name: node.name,
    type: node.type,
  }));
  const sampleCalls = (topology?.calls || []).slice(0, 10).map((call) => ({
    id: call.id,
    source: call.source,
    target: call.target,
    detectPoints: call.detectPoints,
  }));
  return {
    nodeCount,
    edgeCount,
    sampleNodes,
    sampleCalls,
  };
}

function summarizeTraceSearch(traces) {
  const items = traces?.traces || [];
  return {
    count: items.length,
    top: items.slice(0, 5).map((item) => ({
      traceId: item.traceIds?.[0] || null,
      duration: item.duration,
      start: item.start,
      isError: item.isError,
      endpoints: item.endpointNames,
    })),
  };
}

function summarizeTraceDetail(trace) {
  const spans = trace?.spans || [];
  const errorSpans = spans.filter((span) => span.isError);
  return {
    spanCount: spans.length,
    errorSpanCount: errorSpans.length,
    services: [...new Set(spans.map((span) => span.serviceCode).filter(Boolean))],
    endpoints: [...new Set(spans.map((span) => span.endpointName).filter(Boolean))].slice(0, 20),
    slowestSpans: [...spans]
      .map((span) => ({
        serviceCode: span.serviceCode,
        endpointName: span.endpointName,
        peer: span.peer,
        duration: Number(span.endTime) - Number(span.startTime),
        isError: span.isError,
      }))
      .sort((a, b) => b.duration - a.duration)
      .slice(0, 10),
    errorHighlights: errorSpans.slice(0, 10).map((span) => ({
      serviceCode: span.serviceCode,
      endpointName: span.endpointName,
      peer: span.peer,
      duration: Number(span.endTime) - Number(span.startTime),
      logs: (span.logs || []).slice(0, 2),
    })),
  };
}

function toBoolean(value, fallback = true) {
  if (value === undefined) {
    return fallback;
  }
  if (typeof value === "boolean") {
    return value;
  }
  if (["true", "1", "yes", "y"].includes(String(value).toLowerCase())) {
    return true;
  }
  if (["false", "0", "no", "n"].includes(String(value).toLowerCase())) {
    return false;
  }
  throw new Error(`Invalid boolean value: ${value}`);
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function formatDurationTime(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}${pad(date.getMinutes())}`;
}

function defaultDuration(cli) {
  if (cli.start && cli.end) {
    return { start: cli.start, end: cli.end, step: "MINUTE" };
  }
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 60 * 1000);
  return {
    start: cli.start || formatDurationTime(start),
    end: cli.end || formatDurationTime(end),
    step: "MINUTE",
  };
}

function buildEntity(cli) {
  const scope = cli.scope || "Service";
  if (scope !== "Service") {
    throw new Error(`Unsupported scope for dashboard command: ${scope}`);
  }
  if (!cli.service) {
    throw new Error("Missing --service for dashboard command.");
  }
  return {
    scope,
    serviceName: cli.service,
    normal: toBoolean(cli.normal, true),
  };
}

function asInt(value, fallback) {
  if (value === undefined) {
    return fallback;
  }
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) {
    throw new Error(`Invalid integer value: ${value}`);
  }
  return parsed;
}

async function gql(endpoint, query, variables = {}) {
  const result = await postGraphql(endpoint.url, endpoint.headers, { query, variables });
  if (result.errors && result.errors.length) {
    throw new Error(JSON.stringify(result.errors));
  }
  return result.data;
}

const QUERY_FIELD_CACHE = new Map();

async function getQueryFieldMap(endpoint) {
  const cacheKey = endpoint.url;
  if (QUERY_FIELD_CACHE.has(cacheKey)) {
    return QUERY_FIELD_CACHE.get(cacheKey);
  }

  const data = await gql(
    endpoint,
    `query {
      __type(name:"Query") {
        fields {
          name
          args {
            name
          }
        }
      }
    }`
  );

  const fieldMap = new Map(
    (data.__type?.fields || []).map((field) => [
      field.name,
      {
        name: field.name,
        args: (field.args || []).map((arg) => arg.name),
      },
    ])
  );
  QUERY_FIELD_CACHE.set(cacheKey, fieldMap);
  return fieldMap;
}

async function supportsQueryField(endpoint, fieldName) {
  const fieldMap = await getQueryFieldMap(endpoint);
  return fieldMap.has(fieldName);
}

function normalizeServiceRecord(service) {
  if (!service) {
    return null;
  }
  return {
    id: service.id,
    name: service.name || service.serviceCode || service.shortName || null,
    shortName: service.shortName || service.name || service.serviceCode || null,
    layers: service.layers || [],
    group: service.group || "",
    normal: typeof service.normal === "boolean" ? service.normal : true,
  };
}

async function findServiceByExactQuery(endpoint, serviceName) {
  const data = await gql(
    endpoint,
    `query($serviceName:String!) {
      findService(serviceName:$serviceName) {
        id
        name
        shortName
        layers
        group
        normal
      }
    }`,
    { serviceName }
  );
  return normalizeServiceRecord(data.findService);
}

async function findServiceBySearchService(endpoint, serviceName) {
  const data = await gql(
    endpoint,
    `query($serviceCode:String!) {
      searchService(serviceCode:$serviceCode) {
        id
        name
        shortName
        layers
        group
        normal
      }
    }`,
    { serviceCode: serviceName }
  );
  return normalizeServiceRecord(data.searchService);
}

async function findServiceBySearchServices(endpoint, serviceName, duration) {
  const data = await gql(
    endpoint,
    `query($duration:Duration!,$keyword:String!) {
      searchServices(duration:$duration, keyword:$keyword) {
        id
        name
        shortName
        layers
        group
        normal
      }
    }`,
    { duration, keyword: serviceName }
  );
  const matches = (data.searchServices || []).map(normalizeServiceRecord);
  const exact =
    matches.find((item) => item?.name === serviceName) ||
    matches.find((item) => item?.shortName === serviceName);
  return exact || matches[0] || null;
}

async function findServiceByGetAllServices(endpoint, serviceName, duration) {
  const data = await gql(
    endpoint,
    `query($duration:Duration!,$group:String) {
      getAllServices(duration:$duration, group:$group) {
        id
        name
        shortName
        layers
        group
        normal
      }
    }`,
    { duration, group: null }
  );
  const matches = (data.getAllServices || []).map(normalizeServiceRecord);
  const exact =
    matches.find((item) => item?.name === serviceName) ||
    matches.find((item) => item?.shortName === serviceName);
  return exact || matches.find((item) => item?.name?.includes(serviceName)) || null;
}

async function fetchTemplate(endpoint, templateId) {
  const data = await gql(
    endpoint,
    `query($id:String!) {
      getTemplate(id:$id) {
        id
        configuration
      }
    }`,
    { id: templateId }
  );
  if (!data.getTemplate) {
    throw new Error(`Template not found: ${templateId}`);
  }
  return {
    id: data.getTemplate.id,
    configuration: JSON.parse(data.getTemplate.configuration),
  };
}

async function findService(endpoint, serviceName, duration) {
  if (!serviceName) {
    throw new Error("Missing --service for service lookup.");
  }

  const lookupDuration =
    duration ||
    defaultDuration({
      start: "2026-04-02 1708",
      end: "2026-04-02 1808",
    });

  const attempts = [
    {
      enabled: await supportsQueryField(endpoint, "findService"),
      run: () => findServiceByExactQuery(endpoint, serviceName),
    },
    {
      enabled: await supportsQueryField(endpoint, "searchService"),
      run: () => findServiceBySearchService(endpoint, serviceName),
    },
    {
      enabled: await supportsQueryField(endpoint, "searchServices"),
      run: () => findServiceBySearchServices(endpoint, serviceName, lookupDuration),
    },
    {
      enabled: await supportsQueryField(endpoint, "getAllServices"),
      run: () => findServiceByGetAllServices(endpoint, serviceName, lookupDuration),
    },
  ];

  const errors = [];
  for (const attempt of attempts) {
    if (!attempt.enabled) {
      continue;
    }
    try {
      const service = await attempt.run();
      if (service) {
        return service;
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (errors.length) {
    throw new Error(`Service not found: ${serviceName}. Lookup attempts failed: ${errors.join(" | ")}`);
  }
  throw new Error(`Service not found: ${serviceName}`);
}

async function findEndpointInfo(endpoint, serviceId, keyword, duration) {
  if (!keyword) {
    throw new Error("Missing --endpoint for endpoint topology or endpoint trace search.");
  }
  const data = await gql(
    endpoint,
    `query($keyword:String,$serviceId:ID!,$limit:Int!,$duration:Duration) {
      findEndpoint(keyword:$keyword, serviceId:$serviceId, limit:$limit, duration:$duration) {
        id
        name
      }
    }`,
    { keyword, serviceId, limit: 20, duration }
  );
  const matches = data.findEndpoint || [];
  if (!matches.length) {
    throw new Error(`No endpoint matched keyword: ${keyword}`);
  }
  const exact = matches.find((item) => item.name === keyword);
  return exact || matches[0];
}

function flattenTabWidgets(templateConfig, tabName) {
  const tabContainer = (templateConfig.children || []).find((item) => item.type === "Tab");
  if (!tabContainer) {
    throw new Error("No tab container found in template.");
  }
  const tab = (tabContainer.children || []).find((item) => item.name === tabName);
  if (!tab) {
    throw new Error(`Tab not found in template: ${tabName}`);
  }
  return (tab.children || []).filter((item) => item.type === "Widget");
}

function normalizeValuePoint(point) {
  const base = {
    id: point.id,
    value: point.value,
    traceID: point.traceID || null,
  };
  if (point.id && /^\d{13}$/.test(String(point.id))) {
    base.timestamp = new Date(Number(point.id)).toISOString();
  }
  if (point.owner) {
    base.owner = point.owner;
  }
  return base;
}

function normalizeSeries(result) {
  return {
    labels: (result.metric?.labels || []).reduce((acc, item) => {
      acc[item.key] = item.value;
      return acc;
    }, {}),
    values: (result.values || []).map(normalizeValuePoint),
  };
}

async function executeExpression(endpoint, expression, entity, duration) {
  const data = await gql(
    endpoint,
    `query($expression:String!,$entity:Entity!,$duration:Duration!) {
      execExpression(expression:$expression, entity:$entity, duration:$duration) {
        type
        error
        results {
          metric {
            labels {
              key
              value
            }
          }
          values {
            id
            value
            traceID
            owner {
              scope
              serviceID
              serviceName
              normal
              serviceInstanceID
              serviceInstanceName
              endpointID
              endpointName
            }
          }
        }
      }
    }`,
    { expression, entity, duration }
  );
  return data.execExpression;
}

async function executeWidget(endpoint, widget, entity, duration) {
  const expressions = widget.expressions || [];
  const expressionResults = [];
  for (const expression of expressions) {
    const result = await executeExpression(endpoint, expression, entity, duration);
    expressionResults.push({
      expression,
      resultType: result.type,
      error: result.error,
      series: (result.results || []).map(normalizeSeries),
    });
  }

  return {
    title: widget.widget?.title || widget.i,
    widgetType: widget.graph?.type || "Unknown",
    expressions: expressionResults,
  };
}

async function fetchServiceTopology(endpoint, serviceId, duration) {
  const data = await gql(
    endpoint,
    `query($serviceId:ID!,$duration:Duration!) {
      getServiceTopology(serviceId:$serviceId, duration:$duration) {
        nodes {
          id
          name
          type
          isReal
          layers
        }
        calls {
          id
          source
          target
          detectPoints
          sourceComponents
          targetComponents
        }
      }
    }`,
    { serviceId, duration }
  );
  return data.getServiceTopology;
}

async function fetchEndpointTopology(endpoint, endpointId, duration) {
  const data = await gql(
    endpoint,
    `query($endpointId:ID!,$duration:Duration!) {
      getEndpointDependencies(endpointId:$endpointId, duration:$duration) {
        nodes {
          id
          name
          serviceId
          serviceName
          type
          isReal
        }
        calls {
          id
          source
          target
          detectPoints
          sourceComponents
          targetComponents
        }
      }
    }`,
    { endpointId, duration }
  );
  return data.getEndpointDependencies;
}

async function searchTraces(endpoint, condition) {
  const data = await gql(
    endpoint,
    `query($condition:TraceQueryCondition) {
      queryBasicTraces(condition:$condition) {
        traces {
          segmentId
          endpointNames
          duration
          start
          isError
          traceIds
        }
      }
    }`,
    { condition }
  );
  return data.queryBasicTraces;
}

async function fetchTraceDetail(endpoint, traceId) {
  const data = await gql(
    endpoint,
    `query($traceId:ID!) {
      queryTrace(traceId:$traceId) {
        spans {
          traceId
          segmentId
          spanId
          parentSpanId
          serviceCode
          serviceInstanceName
          endpointName
          type
          peer
          component
          isError
          layer
          startTime
          endTime
          tags {
            key
            value
          }
          refs {
            traceId
            parentSegmentId
            parentSpanId
            type
          }
          logs {
            time
            data {
              key
              value
            }
          }
        }
      }
    }`,
    { traceId }
  );
  return data.queryTrace;
}

function detectIntent(cli) {
  if (cli.intent) {
    return cli.intent;
  }
  if (cli.traceId) {
    return "trace-detail";
  }

  const text = `${cli.query || ""} ${cli.tab || ""} ${cli.endpoint || ""}`.toLowerCase();

  if (
    /traceid|trace id|完整trace|完整链路|完整调用链|全链路|span详情|span细节|trace详情|trace detail/.test(
      text
    )
  ) {
    return "trace-detail";
  }

  if (/实例页|实例情况|实例列表|instance/.test(text)) {
    return "instance";
  }

  if (/endpoint 页|endpoint页|接口页|端点页|接口列表|endpoint/.test(text)) {
    return "endpoint";
  }

  if (
    /响应情况|健康情况|健康状态|应用健康|服务健康|应用响应|服务响应|overview|总览|概览|总览页|首页指标|apdex|成功率|success rate|响应时间/.test(
      text
    )
  ) {
    return "overview";
  }

  if (
    /接口链路|接口调用链|endpoint链路|endpoint调用链|接口依赖|接口拓扑/.test(text) &&
    (cli.endpoint || /\/|query|api|controller|endpoint|接口/.test(text))
  ) {
    return "endpoint-topology";
  }

  if (
    /应用链路|服务链路|服务调用链|应用调用链|调用链路|服务拓扑|应用拓扑|依赖关系|上下游依赖|调用拓扑|拓扑/.test(
      text
    )
  ) {
    return cli.endpoint ? "endpoint-topology" : "topology";
  }

  if (
    /慢查询|慢接口|慢请求|性能情况|性能分析|性能问题|响应慢|超时|timeout|耗时|latency|rt|trace|错误请求|异常请求|error/.test(
      text
    )
  ) {
    return "trace-search";
  }

  return "overview";
}

async function main() {
  const cli = parseArgs(process.argv.slice(2));
  const [command, input] = cli._;

  if (!command) {
    printUsage();
    process.exitCode = 1;
    return;
  }

  if (command === "examples") {
    printExamples();
    return;
  }

  if (command === "scenarios") {
    printScenarios(cli.raw);
    return;
  }

  const config = resolveConfig();
  const endpoint = resolveEndpoint(config, cli);

  if (command === "introspect") {
    const payload = {
      query: `
        query IntrospectionQuery {
          __schema {
            queryType { name }
            mutationType { name }
            types {
              name
              kind
            }
          }
        }
      `,
    };
    const result = await postGraphql(endpoint.url, endpoint.headers, payload);
    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        data: result.data,
        errors: result.errors || null,
      },
      cli.raw
    );
    return;
  }

  if (command === "graphql") {
    const payload = readPayload(input);
    const result = await postGraphql(endpoint.url, endpoint.headers, payload);
    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        request: payload,
        data: result.data,
        errors: result.errors || null,
      },
      cli.raw
    );
    return;
  }

  if (command === "template") {
    const templateId = cli.template || input;
    if (!templateId) {
      throw new Error("Missing --template or template id argument.");
    }
    const template = await fetchTemplate(endpoint, templateId);
    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        templateId: template.id,
        configuration: template.configuration,
      },
      cli.raw
    );
    return;
  }

  if (command === "dashboard") {
    const templateId = cli.template || "General-Service";
    const tabName = cli.tab || "Overview";
    const entity = buildEntity(cli);
    const duration = defaultDuration(cli);
    const template = await fetchTemplate(endpoint, templateId);
    const widgets = flattenTabWidgets(template.configuration, tabName);
    const widgetResults = [];
    for (const widget of widgets) {
      widgetResults.push(await executeWidget(endpoint, widget, entity, duration));
    }

    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        templateId,
        tab: tabName,
        entity,
        duration,
        widgetCount: widgetResults.length,
        summary: summarizeDashboard(widgetResults),
        widgets: widgetResults,
      },
      cli.raw
    );
    return;
  }

  if (command === "topology") {
    const kind = cli.kind || "service";
    const duration = defaultDuration(cli);
    const service = await findService(endpoint, cli.service, duration);

    if (kind === "service") {
      const topology = await fetchServiceTopology(endpoint, service.id, duration);
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          kind,
          service,
          duration,
          summary: summarizeTopology(topology),
          topology,
        },
        cli.raw
      );
      return;
    }

    if (kind === "endpoint") {
      const endpointInfo = await findEndpointInfo(endpoint, service.id, cli.endpoint, duration);
      const topology = await fetchEndpointTopology(endpoint, endpointInfo.id, duration);
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          kind,
          service,
          endpoint: endpointInfo,
          duration,
          summary: summarizeTopology(topology),
          topology,
        },
        cli.raw
      );
      return;
    }

    throw new Error(`Unsupported topology kind: ${kind}`);
  }

  if (command === "trace-search") {
    const duration = defaultDuration(cli);
    const service = await findService(endpoint, cli.service, duration);
    const condition = {
      serviceId: service.id,
      queryDuration: duration,
      traceState: cli.traceState || "ALL",
      queryOrder: cli.queryOrder || "BY_DURATION",
      paging: {
        pageNum: 1,
        pageSize: asInt(cli.pageSize, 20),
      },
    };
    if (cli.endpoint) {
      const endpointInfo = await findEndpointInfo(endpoint, service.id, cli.endpoint, duration);
      condition.endpointId = endpointInfo.id;
    }
    if (cli.minDuration !== undefined) {
      condition.minTraceDuration = asInt(cli.minDuration);
    }
    if (cli.maxDuration !== undefined) {
      condition.maxTraceDuration = asInt(cli.maxDuration);
    }
    const traces = await searchTraces(endpoint, condition);
    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        service,
        duration,
        condition,
        summary: summarizeTraceSearch(traces),
        traces,
      },
      cli.raw
    );
    return;
  }

  if (command === "trace-detail") {
    const traceId = cli.traceId || input;
    if (!traceId) {
      throw new Error("Missing --trace-id or trace id argument.");
    }
    const trace = await fetchTraceDetail(endpoint, traceId);
    printJson(
      {
        env: endpoint.envName || null,
        url: endpoint.url,
        traceId,
        summary: summarizeTraceDetail(trace),
        trace,
      },
      cli.raw
    );
    return;
  }

  if (command === "auto") {
    const detected = detectIntent(cli);
    const duration = defaultDuration(cli);

    if (detected === "overview" || detected === "instance" || detected === "endpoint") {
      const tabMap = {
        overview: "Overview",
        instance: "Instance",
        endpoint: "Endpoint",
      };
      const entity = buildEntity(cli);
      const template = await fetchTemplate(endpoint, cli.template || "General-Service");
      const widgets = flattenTabWidgets(template.configuration, tabMap[detected]);
      const widgetResults = [];
      for (const widget of widgets) {
        widgetResults.push(await executeWidget(endpoint, widget, entity, duration));
      }
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          routedTo: "dashboard",
          detectedIntent: detected,
          templateId: cli.template || "General-Service",
          tab: tabMap[detected],
          entity,
          duration,
          widgetCount: widgetResults.length,
          summary: summarizeDashboard(widgetResults),
          widgets: widgetResults,
        },
        cli.raw
      );
      return;
    }

    if (detected === "topology" || detected === "endpoint-topology") {
      const service = await findService(endpoint, cli.service, duration);
      if (detected === "topology") {
        const topology = await fetchServiceTopology(endpoint, service.id, duration);
        printJson(
          {
            env: endpoint.envName || null,
            url: endpoint.url,
            routedTo: "topology",
            detectedIntent: detected,
            kind: "service",
            service,
            duration,
            summary: summarizeTopology(topology),
            topology,
          },
          cli.raw
        );
        return;
      }
      const endpointInfo = await findEndpointInfo(endpoint, service.id, cli.endpoint, duration);
      const topology = await fetchEndpointTopology(endpoint, endpointInfo.id, duration);
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          routedTo: "topology",
          detectedIntent: detected,
          kind: "endpoint",
          service,
          endpoint: endpointInfo,
          duration,
          summary: summarizeTopology(topology),
          topology,
        },
        cli.raw
      );
      return;
    }

    if (detected === "trace-search") {
      const service = await findService(endpoint, cli.service, duration);
      const condition = {
        serviceId: service.id,
        queryDuration: duration,
        traceState: cli.traceState || "ALL",
        queryOrder: cli.queryOrder || "BY_DURATION",
        paging: {
          pageNum: 1,
          pageSize: asInt(cli.pageSize, 20),
        },
      };
      if (cli.endpoint) {
        const endpointInfo = await findEndpointInfo(endpoint, service.id, cli.endpoint, duration);
        condition.endpointId = endpointInfo.id;
      }
      if (cli.minDuration !== undefined) {
        condition.minTraceDuration = asInt(cli.minDuration);
      }
      if (cli.maxDuration !== undefined) {
        condition.maxTraceDuration = asInt(cli.maxDuration);
      }
      const traces = await searchTraces(endpoint, condition);
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          routedTo: "trace-search",
          detectedIntent: detected,
          service,
          duration,
          condition,
          summary: summarizeTraceSearch(traces),
          traces,
        },
        cli.raw
      );
      return;
    }

    if (detected === "trace-detail") {
      const traceId = cli.traceId;
      if (!traceId) {
        throw new Error("Auto routed to trace-detail, but no --trace-id was provided.");
      }
      const trace = await fetchTraceDetail(endpoint, traceId);
      printJson(
        {
          env: endpoint.envName || null,
          url: endpoint.url,
          routedTo: "trace-detail",
          detectedIntent: detected,
          traceId,
          summary: summarizeTraceDetail(trace),
          trace,
        },
        cli.raw
      );
      return;
    }
  }

  throw new Error(`Unknown command: ${command}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
