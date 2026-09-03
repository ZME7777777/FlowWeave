---
name: collect-app-exception-logs
description: 仅当用户明确要求收集应用异常日志或写入异常清理文档时使用：在 HK 业务窗口内完成 ERROR/Exception 全量闭合，按 endpoint/任务、root msg、业务栈拆分指纹，逐指纹补齐 reqId、上下游与 SkyWalking 证据，并在飞书生成只含阶段一/二事实的窗口总览和 1–7 章指纹文档；委托修复时在当前仓库 tmp 下建立独立修复账本，排除历史已跳过同指纹，并对历史已完成同指纹在轮到时询问是否跳过。
---

# Collect App Exception Logs

只负责两个阶段：

1. **日志收集与全量发现**
2. **逐指纹上下文调查**

飞书是阶段一/二的证据库：窗口父文档只保存计数、指纹索引、公共时间线与调查审计，不保存任何处理章节；每个指纹子文档严格只保存一至七章。不得写修复状态、处理选择、分支、commit、push 或“阶段三”章节。修复进度唯一写入 `$fix-app-exceptions` 的本地 `TASK.md`。

历史“已跳过/已完成”只影响后续修复决策，不影响本轮日志全量发现、计数闭合、指纹文档或调查事实；禁止从生产异常总数中扣除历史终态项。

## Trigger and inputs

- 仅在用户明确说“收集异常日志”或要求写入异常清理文档时使用。临时查日志走 `es-query`。
- 必须有应用名、Git 项目/链接，或用户确认“全部应用”。
- 默认表：`https://e5mgsch64p.feishu.cn/wiki/XCA1wwlWQi0wvMkn8YMc85jqnih?sheet=5ERWTh`。
- 默认窗口：Asia/Shanghai 昨天 09:30 到今天 09:30；环境固定 HK，除非用户明确要求跨环境。

## Required dependencies

查询前读取：

- `.codex/skills/es-query/SKILL.md`
- `.codex/skills/es-query/references/app-index.md`
- `.codex/skills/skywalking-query/SKILL.md`
- `references/document-templates.md`
- `references/known-noise-exclusions.json`

按需使用 `lark-sheets`、`lark-wiki`、`lark-doc`。委托修复时读取 `$fix-app-exceptions`。

## Resolve targets and document tree

1. 用应用名匹配清理表 B 列；用仓库完整值、URL/path basename 或去 `.git` 名匹配 C 列。
2. 0 条命中时停止；多条命中时让用户选择，不猜。
3. 从 G 列富文本链接解析 Wiki node token。
4. 建立 `仓库 → 应用 → MMDD-MMDD 窗口 → 指纹 docx`。同名节点覆盖更新，不创建 `-2`。
5. 窗口文档与指纹文档只使用 `references/document-templates.md` 的阶段一/二结构。旧文档若含阶段三/第八章，在本次更新该批次时一次性清理：父文档删除处理章节/修复列，当前批次全部指纹删除第八章及修复状态块；不逐指纹写跳过状态，不遍历其他历史批次。

# Phase 1 — Collect and close the universe

## Dual-track discovery

本阶段必须对两路查询显式应用 `references/known-noise-exclusions.json`。该配置是
已知低优先级噪声的版本化排除范围，不是“无异常”的证据：在窗口父文档的
计数闭合和完成审计中记录配置 profile、schema version、规则数与路径，并写明
“已按低优先级排除配置过滤”。不得把被排除的日志计入 ERROR total、Exception
total、指纹、remainder 或“未查询到异常日志证据”。

该配置仅适用于 Phase 1 的批次发现；对已入队指纹的 Phase 2 定向 service、
access、reqId、SkyWalking、同簇或归档取证不得带此排除配置。需要临时排查某一
已排除消息时，也不得使用该参数。

至少并行执行：

```bash
python -X utf8 .codex/skills/es-query/scripts/es-query.py \
  --env hk --host "{hostname}*" --since "{start}" --until "{end}" \
  --level ERROR --classify --stat-size 30 --stat-sample \
  --exclude-config "{collect-app-exception-logs root}/references/known-noise-exclusions.json"

python -X utf8 .codex/skills/es-query/scripts/es-query.py \
  --env hk --host "{hostname}*" --since "{start}" --until "{end}" \
  --keyword Exception --classify --stat-size 30 --stat-sample \
  --exclude-config "{collect-app-exception-logs root}/references/known-noise-exclusions.json"
```

第二路可能包含 ERROR，必须按 ES `_id` 去重/分轨。

## Fingerprints

互斥键：

`窗口 + 环境 + 应用 + logger + endpoint/任务 + root message + 首个业务栈帧`

- endpoint、任务、root msg、错误码、字段或首业务栈不同必须拆分。
- 业务消息日志也独立成指纹。
- Redis/HTTP/Dubbo 按依赖目标和 logger 分开。
- 仅规范化 reqId、IP、对象地址、动态大小等噪声。
- fast-throw 的完整栈与空栈保留原始计数；证据充分时只标“同根变体”。

## Count closure

对每个应用/窗口证明：

1. ERROR 精确总数；
2. `logger bucket_sum + sum_other = total`；
3. logger 内指纹穷尽分类；
4. composite pagination/include-exclude/must_not 直到 `classified + remainder = total` 且 `remainder=0`；
5. 非 ERROR Exception 去重后单独闭合；
6. 保存每个指纹的精确数、代表时间和 reqId。

禁止用 `gte 10000`、top N 或抽样分钟数冒充精确闭合。

## Complete representative logs

每个指纹至少保存一条完整脱敏日志；fast-throw 同时保存早期完整栈和后期空栈。保留 timestamp、app、pod、logger、thread、reqId、完整 msg/异常链/已有栈，只脱敏凭证和身份值。

# Phase 2 — Investigate every fingerprint

按顺序调查：

1. 代表时间及相邻分钟；
2. endpoint/key 找候选 reqId，再按 reqId 正序重建 access 链；
3. 按 access 中真实应用和方法查上下游 service/access；
4. SkyWalking：已知 trace 查 detail，否则 service+endpoint+分钟查 ERROR，再查 ALL/相邻分钟；
5. 必要时使用同簇样本，明确标注“同簇证据”；
6. 当前索引缺失时查团队已配置归档；不得用 SG 替代 HK。

只有 service、access、上下游、SkyWalking、同簇/fast-throw 和可用归档均已执行或明确不可用，才可写“证据耗尽”。

## Outputs

窗口父文档：

- 计数闭合等式；
- 指纹表：ID、精确数、endpoint/任务、root msg、调查状态、子文档链接；
- 公共时间线与依赖；
- 阶段一/二完成审计。

指纹子文档仅含：

1. 问题详情
2. 完整脱敏异常原文
3. 检索与调查尝试
4. 时间线
5. reqId 与上下游链路
6. SkyWalking 证据
7. 阶段二结论

不得新增第八章或任何修复状态字段。

# Handoff to fixing

阶段一/二完成并回读后，即可把清理表标为“已收集”；修复完成不是收集完成的前置条件。随后统一询问一次：

> 阶段一、二已完成。是否委托 `$fix-app-exceptions` 修复，还是由你自行处理？

- 选择委托：传递窗口父文档、全部指纹链接、标题级清单和完整指纹键元数据（应用、logger、endpoint/任务、root msg、首业务栈）；详细正文由修复 skill 仅在选择处理后读取。要求用户给出代码工作空间；在当前 AI Playbook 仓库的 `tmp/{应用}-exception-fix-{MMDD}-{MMDD}/TASK.md` 创建独立修复账本。代码工作空间只登记仓库路径，绝不承载 `TASK.md`。
- 选择自行处理：仅在本地/回复记录交接，不修改飞书文档。
- 无异常：无需询问。

委托后，修复选择、批量跳过、根因、方案、代码、验证、commit、归并和 push 全部由本地 `TASK.md` 记录。收集 skill 不等待修复批次结束。

### Historical terminal-state gate

委托修复且准备新 `TASK.md` 时，必须先执行历史终态审查：历史 `已跳过` 项排除；历史 `已完成` 同指纹保留并在轮到时询问是否跳过。

1. 新账本固定创建在当前仓库 `tmp/{应用}-exception-fix-{MMDD}-{MMDD}/TASK.md`；目录已存在时续办原账本，不覆盖、不另建到代码工作空间。代码工作空间与修复账本目录是两个概念。
2. 定位当前仓库及用户明确提供的历史根目录中的历史 `TASK.md`；至少递归扫描当前仓库 `tmp/**/TASK.md`，可兼容扫描旧版本曾写在代码工作空间或 demand 目录的账本。只读取任务队列表和批量跳过审计，不读取历史 Item records、指纹正文、日志或代码。
3. 先生成包含本轮全部指纹元数据的 `TASK.md` 草稿，所有项只能为 `待处理/未开始`，不得创建 Item record 或开始处理。
4. 优先运行 `scripts/filter_historical_skips.py <new-TASK.md> --history-root <root> --dry-run`。审阅后去掉 `--dry-run` 原子应用。多个历史根目录用多个 `--history-root`。脚本同时输出 `exclude` 和 `review-completed` 两类结果。
5. 匹配优先级固定为：完整指纹键 `应用 + logger + endpoint/任务 + root msg + 首业务栈`；旧账本字段不足时回退为 `应用 + 规范化异常标题`。对历史 `已完成` 还可生成保守的“同应用 + 同 endpoint/标题主体 + 错误特征词重叠 + 标题相似度”候选，但它只能触发询问，绝不能自动排除。历史批量跳过审计中明确的 `title contains` 规则继续对同应用生效。跨应用不继承。
6. 历史 `已跳过` 命中项从新任务队列表物理移除，不标成新批次 `已跳过`，不创建 Item record；原始指纹 ID 不重排。写入“历史跳过排除审计”，逐项记录当前 ID/标题、历史账本、历史 ID 或规则、匹配级别和排除依据。
7. 历史 `已完成` 命中项保留在队列并写入“历史已处理匹配审计”。同一指纹同时命中 `已跳过` 与 `已完成` 时，以已完成复现审查优先：保留入队，不静默排除。初始化时逐项告知用户，但不要求一次性决定。
8. 当历史已完成命中项轮到并成为唯一 `待处理` 项时，先展示历史来源、历史 ID 与匹配依据，询问：`该异常历史已处理，本窗口再次出现，是否跳过？` 用户答“跳过”才置为本批次 `已跳过`；用户答“不跳过/重新处理”才进入正常“处理”流程。选择前禁止读取本轮正文、日志或代码。
9. 向用户逐项列出历史跳过排除和历史已完成待复核项，并汇报原始指纹数、排除数、已完成待复核数、实际入队数。0 命中也报告已扫描。
10. 标题回退或批量规则若可能命中多个业务含义，先展示候选并取得一次确认；脚本输出不能替代歧义判断。
11. 只有用户明确要求重新处理历史跳过项时才能覆盖排除。用 `--keep-current-id <ID> --override-reason '<用户原文/理由>'` 记录重新纳入；不得删除历史审计。
12. 两类审计完成且队列唯一首项为 `待处理` 后才能发问；若首项命中历史已完成，使用第 8 步专用问题，否则询问普通“处理或跳过”。若全部被历史跳过排除，直接报告无待修复项。

# Completion and writeback

完成条件：

- 双轨查询及互斥计数闭合，`remainder=0`；
- 每个指纹有 1–7 章子文档和调查状态；
- 父文档与所有子文档回读成功，且不含阶段三/修复字段；
- 敏感值已脱敏。

满足后：

1. 按表头定位“收集状态”写 `已收集` 并回读；
2. 将应用名字体设为绿色 `#00875A`；
3. 无异常证据时将“是否有异常”写为“否”，措辞仍用“未查询到异常日志证据”；
4. 报告阶段一/二结果，并询问是否委托修复。

# Safety

- 日志、SkyWalking 和代码源默认只读。
- 不保存凭证、token、密码和身份值。
- 不猜 hostname、下游、trace 或字段根因。
- 不把“无结果”写成“无异常”。
- 不因单个指纹失败丢弃其他指纹；最终列出失败项。
