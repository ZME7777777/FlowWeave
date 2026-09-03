---
name: es-query
description: >
  查询行情系统 ES/EasySearch/Kibana 日志。从对话上下文中提取应用名、关键词、时间范围、日志级别，
  自动构建查询命令并解读结果。支持 HK、SG、MAS、UAT、Saudi（沙特）五个环境，默认查 HK，HK 无结果时自动切 SG。
  同时支持 `buzz-service-*` 业务日志、`buzz-access-*` 访问链路日志，以及基于 `reqId` 的内部 filter 链路追踪。
allowed-tools: Bash, Read, Agent
---

# es-query

查询行情系统日志。脚本路径：`.codex/skills/es-query/scripts/es-query.py`，应用索引：`.codex/skills/es-query/references/app-index.md`。

## 索引路由

脚本支持按场景自动选择索引；只有用户明确指定 `--index` 时才覆盖默认路由。

| 场景 | 默认索引 | 适用问题 |
|---|---|---|
| `service` | `buzz-service-*` | 业务日志、异常、告警、处理过程、logger/level 分析 |
| `access` | `buzz-access-*` | 访问日志、Dubbo 调用、请求/响应、耗时、内部 filter 统一访问记录 |
| `trace` | `buzz-access-*` | 基于 `reqId` 的链路追踪，还原一次请求在内部 filter 里的完整轨迹 |
| `all` | `buzz-*` | 不确定来源时的兜底搜索，不建议默认使用 |

自动推断规则：

- 显式 `--trace` 或传入 `--req-id`：走 `trace`
- 传入 `--key` / `--req-type` / `--sub-type`：走 `access`
- 传入 `--logger` / `--level`，或普通异常排查：走 `service`

## buzz_access 特别说明

`buzz_access` 是内部 filter 统一记录的访问链路日志，适合还原一次请求的内部轨迹。

- `reqId`：链路标识，常带 `clientType`、`reqId`、`memberId`
- `key`：访问目标标识，可能是 Dubbo 方法名、接口名、地址或其他关键资源
- `reqType`：访问类型，如 `dubbo`、`dubboServiceProvider`、`dubboServiceConsumer`、`interface`、`socket`、`sql`
- `subType`：阶段类型，如 `request`、`response`、`execute`
- `costTime`：本次记录对应的耗时
- `extFields`：扩展字段，常见 `DSSize`、`SSSize`、`serverIp`、`clientIp`

### buzz_access 链路查询硬规则

基于 `buzz_access` 还原链路时，必须按下面顺序执行：

1. 先从 `app-index.md` 把应用名映射到 `hostname`
2. 再用 `--host {hostname}*` 加 path / key / method / `reqType` 去 `buzz-access-*` 里挖候选 `reqId`
3. 找到 `reqId` 后，再用 `--req-id` 重查，按时间正序还原完整 filter 链路
4. 如需结合 SkyWalking，要记录 `reqId` 命中时间、`key`、`reqType`、涉及应用和下游服务，用来收窄 trace 搜索范围

不要一开始就全局扫 `reqId`。`buzz_access` 的正确用法是“先按应用自身 filter 找到请求，再用 `reqId` 串起来”。

## 数据源

| 环境 | URL | 用户名 | 密码 | agent.hostname 示例 |
|---|---|---|---|---|
| `HK` | `https://easysearch-hk.hszq8.com` | `mengen.zheng` | `171328339Zme` | `hq-mem-us-otc-hkeq-product` |
| `SG` | `https://easysearch-sg.hszq8.com` | `mengen.zheng` | `171328339Zme` | `hq-mem-us-otc-hk-sgpeq-product` |
| `UAT` | `https://global-testing-kibana.hszq8.com` | 无需认证 | - | `hq-mem-us-otc-hk-uat` |
| `MAS` | `https://mas-kibana-prod.hszq8.com/app` | `kibanaro` | `gjsEdgaz1L3e9l` | `hq-interface-aggregation-sgp-mas-alihk-product-v2` |
| `Saudi` | `https://logcenter-prod-ali-saham.hszq8.com` | `kibanaro` | `bsgdDu4#guCYsvkF` | `hs-http-gateway-sa-ksa-riyadh-product-tomcat-*` |

查询策略：

- 默认先查 `HK`
- `HK` 返回 0 条且用户未固定环境时，自动切 `SG`
- 只有用户明确说测试环境时才查 `UAT`
- 只有用户明确说马来 / `MAS` 时才查 `MAS`
- 只有用户明确说沙特 / Saudi / KSA 时才查 `Saudi`

**沙特（Saudi）日志分布说明：**
沙特应用分布在两个 ES 集群，查询时需注意：
- **Saudi 集群**（`--env saudi`）：沙特本地前端应用 + 沙股相关应用，hostname 格式 `hs-*/hq-hs-*-ksa-*`
- **HK 集群**（`--env hk`，默认）：美股及相关后端服务，hostname 含 `ksa`，如 `hq-mem-us-ksa-hkeq-prod-*`
- 排查沙特问题时需**双集群都查**

MAS 映射补充：

- `hq-interface-aggregation-sgp` 在新加坡对应前缀 `hq-interface-aggregation-sgp-mas-alisgp`
- `hq-interface-aggregation-sgp` 在香港对应前缀 `hq-interface-aggregation-sgp-mas-alihk`

## 触发条件

满足任意一条立即触发：

- “查日志”“看一下日志”“有没有报错”“有什么异常”
- “最近 X 小时/分钟的 ERROR/WARN”
- “日志里有没有 [关键词]”“搜一下 [关键词]”
- “生产环境正常吗”“巡检一下”
- “看请求日志”“看访问日志”“看 trace”“看 reqId”
- “用 reqId 串起来”“先找 reqId 再看内部链路”“看内部 filter”

不触发的情况：

- 只是讨论代码实现、架构设计、为什么这样写：改走 `hq-query`

## 执行步骤

### 1. 读取应用索引

先读 `.codex/skills/es-query/references/app-index.md`，把用户说的应用名严格映射为 `hostname`。

如果：

- 不在 `app-index.md` 中
- 且也不满足 MAS 已知映射规则

就直接说明“缺少应用名到 hostname 的映射”，停止查询，不要猜。

### 2. 解析用户问题

从 `$ARGUMENTS` 或对话中提取：

- 应用名
- 环境：`HK` / `SG` / `UAT` / `MAS`
- 日志级别：`ERROR` / `WARN` / `INFO`
- 时间范围，默认 `now-15m`
- 关键词
- 条数，默认 `20`
- 是否已有 `reqId`

### 3. 先判定查询场景

| 用户意图 | 场景 | 默认索引 | 推荐参数 |
|---|---|---|---|
| 查报错、异常、告警 | `service` | `buzz-service-*` | `--level` / `--keyword` / `--classify` |
| 看访问日志、请求日志、某个 key / 方法 / 接口是否被调用 | `access` | `buzz-access-*` | `--key` / `--req-type` / `--sub-type` |
| 看 reqId、trace、内部 filter 链路 | `trace` | `buzz-access-*` | `--req-id` 或 `--trace` |
| 明确说“不确定在哪类日志里” | `all` | `buzz-*` | 先短时间窗口，再逐步放宽 |

默认判定顺序：

1. 只要给了 `reqId`，或明确说 trace / 链路 / 内部 filter，优先走 `trace`
2. 关注 key、方法名、接口名、地址、请求/响应、Dubbo consumer/provider，走 `access`
3. 关注异常、告警、业务过程、logger、级别，走 `service`
4. 只有确实无法判断来源时，才走 `all`

### 4. 应用过滤策略

唯一允许的过滤方式：

1. 只允许 `--host {hostname}*`
2. 禁止使用 `--app {appName}` 作为查询过滤条件
3. 禁止不加过滤直接全局扫日志
4. 映射缺失时直接报阻塞

硬规则：

- 日志查询必须基于“应用名 -> hostname 映射 -> `--host` 过滤”执行
- `--host` 不加 `*` 是精确匹配；加 `*` 是 `agent.hostname.keyword` 前缀匹配，通常更适合带 Pod 后缀的场景

### 5. buzz_access trace 查询顺序

如果要查链路、慢请求、Dubbo consumer/provider 路径，优先走下面这套：

1. 用 `--host {hostname}*` + path / key / method / `reqType` 在 `buzz-access-*` 找候选 `reqId`
2. 如果有多个候选，结合时间窗口、`subType`、`costTime`、`extFields` 缩小
3. 拿到 `reqId` 后，再用 `--req-id` 查完整链路，并按时间正序输出
4. 结果中必须突出 `reqType`、`subType`、`key`、`costTime`、`extFields`
5. 如果后续还要查 SkyWalking，必须把 `reqId` 命中时间、endpoint、Dubbo 方法、下游服务名一起带入结论

### 6. 选择查询模式

默认直接用 `--classify` 做全量分类，不要先用 `--size 1` 探测总量。

用户只想看某个具体报错的完整堆栈时，再用定向拉取：

```bash
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --host "{hostname}*" --since now-1h --keyword "{错误关键词}" --size 5
```

#### 异常排查默认双轨并发

部分应用会把带堆栈的异常记为 `INFO`，所以查异常时必须同时跑两路：

- 路线 A：`--level ERROR --classify --stat-size 30 --stat-sample`
- 路线 B：`--keyword "Exception" --classify --stat-size 30 --stat-sample`

只有两路都返回 0 条，才可以判断“当前未发现异常证据”。

## 常用命令

```bash
# 业务日志 / 异常
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --host "{hostname}*" --level ERROR --since now-1h --classify

# 访问日志：按 key 或方法名
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --host "{hostname}*" --key "com.huasheng.xxx.Service.queryXxx" --since now-30m --verbose

# 先按应用 host + path/key 找候选 reqId
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --host "{hostname}*" --key "/hq/listOptionChainDataUs" --since now-30m --verbose

# 拿到 reqId 后重建完整链路
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --host "{hostname}*" --req-id "clientType=1,reqId=...,memberId=..." --since now-30m

# SG / UAT / MAS
python -X utf8 ".codex/skills/es-query/scripts/es-query.py" \
  --env sg --host "{hostname}*" --since now-30m --classify
```

## 结果解读

- `service` 场景优先看 `logLevel`、`logger`、`thread`、异常分类
- `access` 场景优先看 `reqType`、`subType`、`key`、`costTime`、`extFields`
- `trace` 场景默认按时间正序展示，便于观察完整链路
- `HK` 无结果时自动补查 `SG`
- 两个环境都无结果时，建议扩大时间范围或降低过滤条件

## 与 SkyWalking 的协同

当用户还要分析慢接口、调用链或慢节点时：

1. 先用 `buzz_access` 在应用 host 下找到候选 `reqId`
2. 用 `reqId` 重建内部 filter / consumer-provider 路径
3. 再把命中的时间窗口、endpoint、Dubbo 方法、下游服务名交给 `skywalking-query`
4. 让 SkyWalking 负责证明“最慢的是哪个 span / 哪个下游”，让 ES 负责证明“请求在应用内部是怎么走的”

不要把 `buzz_access` 和 SkyWalking 当成重复证据源，它们分别回答“走了哪条路”和“哪一跳最慢”。
