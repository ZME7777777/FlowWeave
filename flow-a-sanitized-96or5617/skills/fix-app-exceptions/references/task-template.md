# Exception Fix Task List

> `TASK.md` 是修复过程唯一事实源。飞书仅提供阶段一/二证据，不写修复进度。每次状态或确认变化后立即更新。

## Batch

- 修复工作区：{当前 AI Playbook 仓库下 tmp/{异常收集应用}-exception-fix-{MMDD}-{MMDD}；禁止使用代码工作空间}
- 用户指定代码工作空间：{absolute path / 待用户提供}
- 仓库布局：{flat/relative/待确认}
- 异常收集应用：{来自阶段一/二输入；不是实际修复仓库名}
- 原始异常窗口：{start/end with source timezone}
- 规范化批次日期：{MMDD-MMDD；不含年份、小时、分钟}
- 标准基准分支：`feature/exception-fix-baseline-{异常收集应用}-{MMDD}-{MMDD}`
- 阶段一/二窗口父文档：{link/path}
- 阶段一/二异常全集：{count；历史排除不改变此数}
- 修复候选队列：原始={count}；历史跳过排除={count}；历史已完成待复核={count}；实际入队={count}
- 仓库分支登记：{repo -> origin default/commit -> task start/return ref -> baseline 未创建/已存在/因实际改动延迟创建 -> current HEAD -> task cleanup -> push eligibility/state}

## Rules

- 未轮到的项只保留标题级信息。
- 处理项先读父文档和当前子文档 1–7 章，再准备代码。
- 修改、提交归并和 push 分别等待明确门禁。
- 批量跳过只按队列表元数据匹配；多个条件按 AND 组合且文本忽略大小写。一次本地原子更新，不读正文/代码、不写飞书。
- 新批次激活首项前先扫描历史 `TASK.md`；历史已跳过同应用指纹从队列物理移除并逐项审计，不重复记为本批次已跳过。
- 历史已完成同指纹保留入队并逐项审计；轮到时先询问“本窗口再次出现，是否跳过”，未经选择不读取正文或代码。
- 代码准备只建任务分支，不为分析预建基准；基准只在真实改动提交成功后按需创建。
- 所有仓库使用由异常收集应用和 `MMDD-MMDD` 构造的同名基准；禁止使用修复仓库名或带年份/时分的窗口。
- 任务分支永不 push；无改动结论或成功归并后立即安全删除；批次末只 push 含实际修复 commit 的基准。

## Status model

- 主状态：`未开始/待处理/处理中/阻塞/已完成/已跳过`
- 执行阶段：`不适用/代码准备/分析/方案确认/修改验证/提交归并`
- 代码准备子状态：`不适用/待工作空间/待仓库确认/同步中/分支准备中/已完成/阻塞`
- 提交归并子状态：`不适用/待选择/提交中/归并中/已归并/已覆盖/暂不提交/阻塞`
- 任务分支清理状态：`不适用/待判断/待清理/已删除/保留-阻塞`

标准流转：

```text
未开始 → 待处理 → 处理中[代码准备 → 分析 → 方案确认 → 修改验证 → 提交归并] → 已完成
待处理/未开始 → 已跳过（单项或批量明确授权）
任一执行阶段失败 → 阻塞 → 回到原阶段
```

## Task queue

| 顺序 | ID | 应用/仓库 | 异常指纹标题 | logger | endpoint/任务 | root msg | 首业务栈 | 阶段一/二证据 | 主状态 |
|---:|---|---|---|---|---|---|---|---|---|
| 1 | F001 | {app/repo} | {title} | {logger} | {endpoint/task} | {root msg} | {first business frame/无} | {link/path} | 待处理 |
| 2 | F002 | {app/repo} | {title only} | {logger} | {endpoint/task} | {root msg} | {first business frame/无} | {link/path} | 未开始 |

> ID 沿用阶段一/二指纹 ID。历史排除后不重排；序号可保留原顺序。被排除项不出现在本表。

## Historical skip exclusion audit

- 扫描时间：{time}
- 扫描范围：{roots}
- 历史账本：扫描 {count} 个 `TASK.md`；只读取队列表和批量跳过审计，未读取证据正文、日志或代码。
- 排除规则：完整指纹键优先；旧账本回退到应用+规范化标题；同应用历史批量标题规则继续生效。
- 汇总：原始={count}；排除={count}；实际入队={count}；明确重新纳入={IDs/无 + 用户授权原文}。

| 当前指纹ID | 应用 | 异常指纹标题 | 历史来源 | 历史ID/规则 | 匹配级别 | 排除依据 |
|---|---|---|---|---|---|---|
| F003 | {app} | {title} | {historical TASK.md} | {F008/rule} | {完整指纹键/旧账本标题回退/历史批量规则} | {reason} |

> 即使 0 命中也保留本节。历史排除项只出现在本审计，不创建 Item record。

## Historical completed match audit

- 汇总：历史已处理同指纹={count}；这些指纹保留在本批次队列，不自动排除。
- 处理门禁：命中项成为唯一 `待处理` 时，先展示历史来源并询问用户“该异常历史已处理，本窗口再次出现，是否跳过？”。
- 冲突优先级：同时命中历史 `已跳过` 与 `已完成` 时，以已完成复现审查优先，保留入队并询问。

| 当前指纹ID | 应用 | 异常指纹标题 | 历史来源 | 历史ID | 匹配级别 | 入队依据 |
|---|---|---|---|---|---|---|
| F004 | {app} | {title} | {historical TASK.md} | {F002} | {完整指纹键/旧账本标题回退/旧账本标题相似候选} | 历史已完成；保留并在轮到时询问是否跳过 |

> 即使 0 命中也保留本节。用户选择跳过后，按本批次跳过写入队列和 Batch skip audit；选择重新处理后才创建 Item record 并读取证据。

## Batch skip audit

每次批量跳过追加一条紧凑记录：

- `{batch-id}`：时间={time}；原始确认=`{text}`；条件=`{ids/app/title；可组合}`；命中=`{IDs}`；排除=`{IDs + reason}`；预读=`未读取正文/代码`；下一项=`{ID/无}`。

无需为每个批量跳过项复制相同长段落。

## Existing worktree changes

- 初始化 `git status --short`：{repo -> existing changes}

## Item records

### F001

- 处理选择：{待选择 / 处理确认文本和时间 / 单项跳过 / 批量跳过 batch-id}
- 主状态：{status}
- 执行阶段：{phase}
- 阻塞阶段、原因与解除条件：{...}
- 应用名：{app}
- 阶段一/二证据：{parent + child links}
- 文档读取状态：{父文档 + 子文档 1–7 章；旧第八章忽略}
- 全链路因果链：{root origin → propagation → surface/secondary exception}
- 逐跳证据：{direct/cluster/code inference/unverified + source}
- 证据缺口与排除项：{...}
- 代码来源集合：{app/repo + role + evidence + include/exclude reason}
- 动态扩展记录：{source location + candidate + user confirmation + sync result}
- 仓库匹配：{find-and-pull-hq-git result and confirmation}
- 代码同步：{cloned/fetched/failed}
- 远端默认基线：{repo -> branch/commit}
- 标准基准分支：{repo -> `feature/exception-fix-baseline-{异常收集应用}-{MMDD}-{MMDD}` + 未创建/已存在/因实际改动延迟创建 + initial/current HEAD + no upstream}
- 任务分支：{repo -> branch + exact start commit + return ref + no upstream + never push}
- 实际改动判定：{repo -> 有/无 + files/diff/covered evidence；分析或读取调用链不算改动}
- 任务分支清理：{repo -> 待判断/待清理/已删除/保留-阻塞 + command/evidence/reason}
- 代码准备子状态：{status}
- 问题现象、根因与置信度：{...}
- 必须消除的故障 vs 应保持的原结果：{异常/错误噪音/超时等 vs 调用方已依赖的数据、空值、降级结果和副作用}
- 调用方矩阵：{caller -> 输入/场景 -> success/error -> model/data/null/empty -> 结构/排序/缓存/下游调用/副作用/可观测性；未知调用方边界}
- 原可观察结果基线：{与本项有关的 success/errorCode/errorList/errorMsg、model/data、null/空集合/默认值、响应结构、排序分页、缓存、调用次数、副作用、日志/指标/trace}
- 候选修复层比较：{根因产生点 / 服务或契约边界 / 调用方消费边界 -> 故障消除能力、消费者语义差异、影响面、风险、验证难度、选择/排除理由}
- 修复责任点与结果保真方案：{为何是消除故障所需语义差异最小的方案；代码量最小不是首要标准}
- 改动前后逐调用方行为对比：{明确唯一允许变化；其余逐项保持}
- 保持不变：{...}
- 预计改动：{files/logic/tests}
- 方案确认：{text/time}
- 实际改动与文件：{...}
- 验证命令、结果、残余风险：{故障回归 + 每类调用方契约断言 + 正常路径保真 + 未知调用方/生产验证边界}
- 建议提交信息：`fix: xxxxx [HST_AI_Tag: AI_Generated]`
- 提交归并选择：{text/time}
- 提交归并子状态：{status; covered commit/evidence when applicable}
- 任务 Commit：{repo -> hash/message/files/hooks}
- 基准归并：{repo -> before/after + ff-only + ancestor check}
- 基准创建依据：{repo -> actual fix commit；无改动仓库必须为未创建/既有基准未变化}
- 合并阻塞与用户处理复核：{...}
- Push 资格：{repo -> 纳入/不纳入 + actual batch commits + task branch deleted evidence}
- 批次 Push：{final choice/result}

> 批量跳过项可只在队列表和“Batch skip audit”登记，不创建 Item record。
