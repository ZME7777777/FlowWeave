---
name: skywalking-query
description: >
  查询 SkyWalking 分布式链路追踪，支持 traceId 查询、服务拓扑、endpoint 依赖、慢请求和错误分析。
  支持 HK（香港）、GSC（新加坡）、Riyadh（利雅得）三个环境。
  触发条件——以下任意一条立即触发：
  - 用户说"查一下 SkyWalking"、"看一下这个 traceId"
  - 用户询问调用链路、链路追踪、span、拓扑
  - 用户说"慢在哪个 span / 哪个下游"
  - 用户说"看看服务拓扑"、"看下游依赖"
  - 用户说"某个 endpoint 最近有没有慢请求 / 错误"
allowed-tools: Bash, Read, Agent
---

# SkyWalking Query

查询行情系统分布式链路追踪。脚本路径：`.codex/skills/skywalking-query/scripts/sw-query.js`，配置文件：`.codex/skills/skywalking-query/config.json`。

## 数据源

| 环境 | GraphQL URL |
|---|---|
| **HK（默认）** | `https://skywalking-saman-hkeq.hszq8.com/graphql` |
| **GSC** | `https://gsc-skywalking.hszq8.com/graphql` |
| **Riyadh** | `https://skywalking-riyadh.hszq8.com/graphql` |

配置读取顺序：`.codex/skills/skywalking-query/config.json` → `.codex/skills/skywalking-query/config.template.json`。

---

## 触发条件（满足任意一条立即触发）

| 场景 | 触发示例 |
|---|---|
| traceId 查询 | "看一下这个 traceId"、"查 traceId xxx" |
| 调用链路 | "这个接口调用链路是什么"、"链路追踪"、"span 慢在哪" |
| 服务拓扑 | "看看服务拓扑"、"下游依赖"、"服务依赖关系" |
| 慢请求/错误 | "最近有没有慢请求"、"最近有没有错误 trace"、"超时 trace" |
| 综合排查 | "从 SkyWalking 看一下依赖调用"、"查一下 SkyWalking" |

**不触发的情况：**
- 主要查日志内容 → 走 `es-query`
- 主要查指标/告警 → 走 `prometheus-observer`

---

## 场景路由

使用前先确认场景，按下表选择命令：

| 场景 | 命令 | 典型参数 |
|---|---|---|
| `Overview` 页总览指标 | `dashboard` | `--template General-Service --tab Overview --service <service>` |
| `Instance` 页实例列表和指标 | `dashboard` | `--template General-Service --tab Instance --service <service>` |
| `Endpoint` 页接口列表和指标 | `dashboard` | `--template General-Service --tab Endpoint --service <service>` |
| 服务调用拓扑、上下游依赖 | `topology` | `--kind service --service <service>` |
| 某个 endpoint 的依赖拓扑 | `topology` | `--kind endpoint --service <service> --endpoint <keyword>` |
| 某服务最近慢请求、错误 trace | `trace-search` | `--service <service> --trace-state ERROR\|ALL --query-order BY_DURATION` |
| 已知 `traceId`，查完整 span 链路 | `trace-detail` | `--trace-id <traceId>` |

不确定场景时，运行：

```bash
node ".codex/skills/skywalking-query/scripts/sw-query.js" scenarios
```

---

## 执行步骤

### 第一步：确认环境和目标

从用户请求中提取：
- **环境**：HK / GSC / Riyadh，未指定默认 HK
- **目标类型**：traceId、service、endpoint、topology
- **时间范围**：未指定默认最近 30 分钟
- **症状**：慢、超时、错误、依赖失败
- **可选过滤**：关键词、状态码、时长阈值、下游服务

若用户只描述业务现象，先从本地文档或代码元数据确认 service 名称，再查询。

### 第二步：执行查询

常用命令：

```bash
# 查看示例
node ".codex/skills/skywalking-query/scripts/sw-query.js" examples

# 查看场景路由
node ".codex/skills/skywalking-query/scripts/sw-query.js" scenarios

# Schema 探查（确认版本兼容性）
node ".codex/skills/skywalking-query/scripts/sw-query.js" introspect --env hk

# Dashboard Overview 指标
node ".codex/skills/skywalking-query/scripts/sw-query.js" dashboard \
  --env hk \
  --service hq-interface-hkeq-product \
  --template General-Service \
  --tab Overview \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"

# 服务拓扑
node ".codex/skills/skywalking-query/scripts/sw-query.js" topology \
  --env hk \
  --service hq-interface-hkeq-product \
  --kind service \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"

# 错误 trace 搜索
node ".codex/skills/skywalking-query/scripts/sw-query.js" trace-search \
  --env hk \
  --service hq-interface-hkeq-product \
  --trace-state ERROR \
  --query-order BY_DURATION \
  --page-size 20 \
  --start "2026-04-01 2112" \
  --end "2026-04-01 2142"

# 按 traceId 查详情
node ".codex/skills/skywalking-query/scripts/sw-query.js" trace-detail \
  --env hk \
  --trace-id <traceId>

# 自动路由（auto）
node ".codex/skills/skywalking-query/scripts/sw-query.js" auto \
  --env hk \
  --service hq-interface-hkeq-product \
  --query "查 overview 指标"
```

时间格式：`YYYY-MM-DD HHmm`（如 `2026-04-01 2112`）

### 第三步：Schema 版本适配

SkyWalking GraphQL schema 因版本不同存在差异，按以下优先级确定查询形态：

1. 若团队已有可用 payload，直接复用
2. 运行 `introspect` 检查 schema
3. 若 introspection 被禁用，从 SkyWalking UI 网络流量复制 GraphQL 请求体

详见 `.codex/skills/skywalking-query/references/query-recipes.md`。

### 第四步：汇总结果

回复结构：

1. 确认目标和环境
2. 关键 trace 或拓扑证据
3. 最慢或失败的 hop
4. 疑似瓶颈层
5. 下一步验证方向（建议联动 es-query 或 prometheus-observer）

---

## 输出规范

- 先说结论，再给背景说明
- 优先使用 trace 证据，不做无依据推断
- 若 schema 从 UI 流量反推，明确说明
- 除非用户要求输出报告，否则不写文件
