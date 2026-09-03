# 多应用异常收集与修复 Flow 设计

> 状态：`DRAFT`，供流程闭环评审。  
> 范围：把 `collect-app-exception-logs` 从单应用入口拆成 FlowWeave 可治理的能力与节点；保留其证据、修复和人工确认语义。  
> 非目标：本设计不创建节点、流程、环境或外部文档，也不改变生产日志、SkyWalking、Git 仓库或飞书数据。

## 1. 结论与不变约束

新能力的批次单位是“应用表 + 固定时间窗 + 固定环境”，不是把所有应用的异常混成一个异常集合。每个应用仍是独立的收集、闭合、调查、文档和修复边界。

以下规则是从原 Skill 继承的不可变约束：

1. 每个应用必须先按 `ERROR` 和 `Exception` 双轨查询；第二轨与第一轨按 ES `_id` 去重后独立计数。
2. 每个应用必须满足 `logger bucket_sum + sum_other = total`、`classified + remainder = total` 且 `remainder=0`，才能称为 Phase 1 完成；抽样、`gte 10000` 或 top-N 不能替代闭合。
3. 指纹键固定为 `窗口 + 环境 + 应用 + logger + endpoint/任务 + root message + 首业务栈帧`。不同应用绝不合并，跨应用也不继承历史跳过规则。
4. 每一个指纹仍要完成代表日志、`reqId`/access 上下游、SkyWalking、同簇或归档调查；只有全部证据源执行或明确不可用，才能标记“证据耗尽”。
5. 飞书仅保存 Phase 1/2 的窗口父文档与 1–7 章指纹文档。修复选择、代码、验证、commit、merge 和 push 只能保存到本地 `TASK.md`。
6. 历史已跳过只影响修复队列，不影响异常全集、计数、指纹或飞书证据；历史已完成只能触发复现询问，不能自动排除。
7. 修改代码前仍必须先得到“处理”选择和结果保真方案确认；提交、归并、push 仍须分别得到明确确认。

因此，本设计只使用一个 Flow A：自动完成**批量目标解析、证据收集、调查、文档回读、状态回写和修复账本准备**，随后在同一 FlowRun 内进入受 Gate 控制的修复阶段。包含业务语义取舍或外部 Git 写入的步骤保留人工 Gate，不能被自动运行绕过。

## 2. FlowWeave 适配边界

FlowWeave 节点资产当前只支持 `URL` 和 `FILE` 输入输出字段。因此本设计将 JSON、Markdown、CSV 等结构化中间数据作为 `FILE` Artifact 传递；不使用提示词中的临时 JSON、会话上下文或节点工作目录作为跨节点事实源。

Flow 定义是静态 DAG，端口映射不能从一份应用表自动扩展为未知数量的 Flow 节点。为支持“表中所有应用”，批量节点会读取冻结的 `target-manifest.json`，并使用 OpenHands **正式 Task 子 Agent** 作为每应用工作单元。它不创建 FlowWeave 私有 foreach 执行器，也不按文本或顺序猜测 Task、事件或结果身份。

第一版默认每批次顺序处理应用；并发数可在批次输入中显式设为有限正数。每个子任务只写入它自己的 `apps/<app-key>/` 工作目录，父节点只从已校验的结果文件聚合。某一应用失败不会丢弃或阻塞其他应用，但批次报告必须如实列出失败和未闭合状态。

所有 ES、SkyWalking、飞书和 Git 授权均由平台 Secret Reference 或已治理的连接提供。不得把连接密码、token、身份值或本地配置文件打包进 Skill、Context、节点提示词、Artifact 或日志。

## 3. 受治理能力设计

下表的每项都作为独立的 FlowWeave Capability Version 导入；版本、digest 和依赖在 FlowRun Snapshot 中冻结。

| Capability key | 类型 | 责任 | 禁止事项 |
|---|---|---|---|
| `exception-target-resolution` | Skill | 读取应用表，匹配应用/仓库，使用冻结的 app index 解析 hostname、Wiki 链接与时间窗 | 映射缺失或多条命中时猜测目标 |
| `exception-es-closure` | Skill | 单应用双轨 ES 查询、去重、全量分类、计数闭合、代表日志脱敏 | 用样本、top-N 或非 hostname 过滤替代闭合 |
| `exception-trace-investigation` | Skill | 单应用逐指纹 access/reqId、上下游、SkyWalking、同簇/归档调查 | 用 SG 代替 HK，或无证据推断 trace/下游 |
| `exception-evidence-publishing` | Skill | 按既有模板更新/回读 Wiki/docx 与清理表 | 写入阶段三、修复状态或创建同名 `-2` 文档 |
| `exception-repair-ledger` | Skill | 按应用生成或续办本地修复账本，并执行历史终态审查 | 读取待选择项的正文/代码，或跨应用匹配历史项 |
| `exception-repair-analysis` | Skill | 对获准的单个指纹重建因果链、调用方矩阵和结果保真方案 | 确认前修改代码或创建提交 |
| `exception-repair-implementation` | Skill | 在已批准范围内修复、验证、准备提交/归并信息 | 自动 commit、merge、push，或越权修改仓库 |
| `hq-app-index` | Context | 应用名到 hostname 的只读映射及环境路由规则 | 保存连接凭据或被节点自行覆盖 |
| `exception-document-templates` | Context | 窗口父文档、指纹子文档和修复账本模板 | 作为可变运行时模板修改历史证据 |

原有 `es-query`、`skywalking-query`、`lark-sheets`、`lark-wiki`、`lark-doc` 和 `find-and-pull-hq-git` 是上述 Skill 的正式依赖能力；不需要再由 FlowWeave 自建 HTTP 或 Git 执行协议。

## 4. Artifact 契约

所有文件采用 UTF-8、稳定 JSON key 排序、ISO-8601 时间和 `Asia/Shanghai` 显式时区。每个文件含 `schema_version`、`batch_id`、创建者节点与输入 Artifact 的内容 hash，方便重试和审计。

### 4.1 `target-manifest.json`

每个条目至少含：

```json
{
  "app_key": "hq-interface",
  "application": "hq-interface",
  "repository": "hq-interface",
  "hostname_pattern": "hq-interface-hkeq-product-tomcat*",
  "environment": "HK",
  "window": {"start": "2026-09-02T09:30:00+08:00", "end": "2026-09-03T09:30:00+08:00"},
  "source_row": {"sheet_url": "<redacted>", "row_id": "provider-id"},
  "wiki_node": {"id": "provider-id"},
  "state": "READY"
}
```

这里的 `app_key` 是批次内稳定键；文档内的 `F001` 等指纹 ID 仍在**每个应用内**编号。跨应用引用使用 `app_key/F001`，绝不把各应用的 F 编号重新编成全局序号。

### 4.2 `phase1-bundle.json`

按 `apps[app_key]` 保存双轨查询条件、ES `_id` 去重信息、总数与闭合等式、logger bucket、指纹候选、代表日志引用、失败原因和每项状态。只有状态为 `CLOSED` 的应用可进入 Phase 2。

### 4.3 `phase2-evidence-bundle.json`

按应用和指纹保存完整脱敏日志、调查尝试、时间线、reqId/access 链、上下游、SkyWalking、同簇或归档、置信度与证据缺口。每个指纹只允许 `CONFIRMED`、`HIGH_CONFIDENCE`、`EVIDENCE_EXHAUSTED` 或显式 `FAILED` 状态；不得用空结果改写成“无异常”。

### 4.4 `evidence-index.json` 与 `repair-intake.json`

`evidence-index.json` 保存每个应用窗口父文档与每个指纹子文档的 provider ID/URL、模板校验和回读结果。`repair-intake.json` 仅含标题级清单、完整指纹键元数据和证据链接；它不复制详细日志，也不包含修复状态。

## 5. 节点资产与 Skill 设计

节点的字段 key 是长期端口契约。括号内标明字段类型。

### N1：批次目标解析

- 节点资产：`exception-batch-target-resolution`
- 绑定 Skill：`exception-target-resolution`、`hq-app-index`
- 输入：`application_sheet`（URL）、`batch_options`（FILE）
- 输出：`target_manifest`（FILE）、`target_resolution_report`（FILE）
- 自动行为：读取应用表，按原规则匹配 B/C 列，解析 Wiki node；冻结 HK 与时间窗，校验应用/仓库/hostname 的一一对应关系。
- Gate：0 条或多条匹配、缺 hostname/Wiki 链接、非法时间窗时输出 `BLOCKED` 清单并停止，不猜测或静默跳过。

### N2：批量异常全集收集

- 节点资产：`exception-batch-phase1-collection`
- 绑定 Skill：`exception-es-closure`、`hq-app-index`
- 输入：`target_manifest`（FILE）
- 输出：`phase1_bundle`（FILE）、`phase1_execution_report`（FILE）
- 自动行为：对每个 `READY` 应用创建正式 OpenHands Task 子 Agent。每个任务严格执行 ERROR/Exception 双轨、ES `_id` 去重、logger/指纹穷尽分类、`remainder=0` 证明、完整脱敏代表日志保存。
- 成功标准：单应用 `CLOSED`；无日志仅在两轨均为 0 时标记 `NO_EVIDENCE`，仍保存查询条件与闭合记录。
- 失败隔离：权限、索引或映射异常写为该应用 `FAILED`，继续处理其余应用；不得删除失败项或把批次伪装为全量完成。

### N3：批量逐指纹调查

- 节点资产：`exception-batch-phase2-investigation`
- 绑定 Skill：`exception-trace-investigation`、`skywalking-query`
- 输入：`phase1_bundle`（FILE）
- 输出：`phase2_evidence_bundle`（FILE）、`phase2_execution_report`（FILE）
- 自动行为：仅对 `CLOSED` 应用的全部指纹分派正式 Task 子 Agent；按“代表时间/相邻分钟 → 应用 host 下 endpoint/key 找 reqId → reqId 正序 access 链 → 上下游 → SkyWalking ERROR/ALL → 同簇/归档”的顺序执行。
- 成功标准：每个指纹具备一个允许的调查状态和全部尝试记录。Phase 1 失败的应用被保留为 `NOT_STARTED_DEPENDENCY_FAILED`，不是“无异常”。

### N4：阶段一/二证据发布与回读

- 节点资产：`exception-evidence-publish-and-verify`
- 绑定 Skill：`exception-evidence-publishing`、`exception-document-templates`
- 输入：`target_manifest`（FILE）、`phase1_bundle`（FILE）、`phase2_evidence_bundle`（FILE）
- 输出：`evidence_index`（FILE）、`publish_receipt`（FILE）
- 自动行为：按 `仓库 → 应用 → MMDD-MMDD → 指纹 docx` 更新同名节点；旧批次如有阶段三/第八章，按原规则一次性清理当前批次的旧修复字段；写入后逐篇回读、验证模板边界与脱敏。
- 写回：仅当 Phase 1/2 对该应用全部完成时，按表头写 `收集状态=已收集`、将应用名标绿；无日志使用“未查询到异常日志证据”，不改写为“无异常”。
- Gate：任何文档回读失败则该应用保持未收集并记录失败；不得先写成功状态。

### N5：批次闭合审计与修复入口生成

- 节点资产：`exception-batch-closure-and-repair-intake`
- 绑定 Skill：`exception-repair-ledger`（只使用其初始化前能力）
- 输入：`target_manifest`（FILE）、`phase1_bundle`（FILE）、`phase2_evidence_bundle`（FILE）、`evidence_index`（FILE）、`code_workspace_manifest`（FILE，可选）
- 输出：`batch_completion_report`（FILE）、`repair_intake`（FILE）、`repair_ledger_index`（FILE）
- 自动行为：汇总“成功、无异常证据、失败、未开始”的应用及指纹数；对每个已完成且有异常的应用独立创建/续办 `tmp/{应用}-exception-fix-{MMDD}-{MMDD}/TASK.md` 草稿，扫描历史账本，产出历史跳过排除审计和历史已完成待复核审计。
- 关键约束：修复账本仍按应用分别创建，保持原分支命名 `feature/exception-fix-baseline-{异常收集应用}-{MMDD}-{MMDD}`；不能把多个应用放入同一账本或基准分支。
- Gate：没有用户提供的代码工作空间时，可生成修复入口和待补项，但不得读取代码、创建任务分支或开始分析。

### N6：修复范围确认（人工 Gate）

- 节点资产：`exception-repair-decision-gate`
- 输入：`repair_intake`（FILE）、`repair_ledger_index`（FILE）、`batch_completion_report`（FILE）
- 输出：`repair_decisions`（FILE）
- 人工动作：用户选择“委托修复 / 自行处理 / 本批次结束”，并可对历史已完成重现项选择跳过或重新处理。委托修复时，每次只为各应用账本的唯一当前指纹生成一个 `repair_decision`。
- Gate：没有 `repair_decisions` 不进入任何代码读取、Git 同步、分支创建或代码修改。它保留原入口在 Phase 2 后统一询问一次的语义。

### N7：单应用修复分析与方案（受 Gate 的自动节点）

- 节点资产：`exception-app-repair-analysis`
- 绑定 Skill：`exception-repair-analysis`、`find-and-pull-hq-git`
- 输入：`repair_decisions`（FILE）、`repair_intake`（FILE）、`repair_ledger_index`（FILE）
- 输出：`repair_proposals`（FILE）、`repair_analysis_receipt`（FILE）
- 执行范围：只处理用户在 N6 选择处理的当前应用/当前指纹；读取 1–7 章证据，准备本地无 upstream 任务分支，建立因果链、调用方矩阵、候选修复层比较和结果保真方案。
- Gate：输出方案后必须人工确认。方案未确认时不得编辑代码、测试、配置或依赖。

### N8：单应用修复实施、验证与提交选择（受 Gate 的自动节点）

- 节点资产：`exception-app-repair-implementation`
- 绑定 Skill：`exception-repair-implementation`
- 输入：`approved_repair_proposals`（FILE）
- 输出：`repair_validation_report`（FILE）、`commit_merge_options`（FILE）
- 自动行为：仅在批准范围内修改和验证；把证据、diff、验证和风险写回对应应用的 `TASK.md`。
- Gate：节点结束于“待提交并归并”。之后的 commit/ff-only merge，以及批次末 push，分别由独立人工 Gate 批准；任务分支永不 push。

## 6. 流程编排

本设计只创建一个 Flow A：`批量应用异常收集、证据与修复闭环`。它的前半段由自动运行完成；后半段仍属于同一个 FlowRun，但由原 Skill 已有的人工 Gate 驱动。

```text
N1 目标解析
  │ target_manifest
  ▼
N2 Phase 1 批量收集
  │ phase1_bundle
  ▼
N3 Phase 2 批量调查
  │ phase2_evidence_bundle
  ├────────────────────────────────────────┐
  ▼                                        │
N4 证据发布与回读  ◄── target_manifest ───┘
  │ evidence_index
  ▼
N5 批次闭合审计与修复入口
  ├─ 无异常 / 仅失败项 → batch_completion_report（终端报告）
  └─ 有待修复项
        │ repair_intake + repair_ledger_index
        ▼
      N6 修复范围确认（人工 Gate）
        │ repair_decisions
        ▼
      N7 单应用、单指纹分析与方案
        │ repair_proposals
        ▼
      方案确认 Gate（人工）
        │ approved_repair_proposals
        ▼
      N8 修改与验证
        │ commit_merge_options
        ▼
      提交/归并 Gate；全部应用账本终态后 Push Gate（人工）
        ▼
      batch_completion_report（终端报告）
```

控制边为 `N1 → N2 → N3 → N4 → N5 → N6 → N7 → N8`；无修复项时 N5 直接终止。端口映射如下：

| 源字段 | 目标字段 |
|---|---|
| N1.`target_manifest` | N2.`target_manifest`、N4.`target_manifest`、N5.`target_manifest` |
| N2.`phase1_bundle` | N3.`phase1_bundle`、N4.`phase1_bundle`、N5.`phase1_bundle` |
| N3.`phase2_evidence_bundle` | N4.`phase2_evidence_bundle`、N5.`phase2_evidence_bundle` |
| N4.`evidence_index` | N5.`evidence_index` |
| N5.`repair_intake`、`repair_ledger_index` | N6 对应输入 |
| N6.`repair_decisions` | N7.`repair_decisions` |
| N7.`repair_proposals` | 方案确认 Gate 的输入 |
| 方案确认 Gate.`approved_repair_proposals` | N8.`approved_repair_proposals` |

N1 的 `application_sheet`、`batch_options` 与 N5 的可选 `code_workspace_manifest` 是运行时输入 Artifact。N1–N5 使用自动运行，能力、Context、门禁和输入都在自动运行草稿中冻结。N6–N8 则在**同一 FlowRun**中按当前应用账本的唯一待处理指纹创建下一组 NodeRun/Attempt；它们不是另一个 Flow，也不会新建第二个 FlowRun。

修复队列的重复不是以 Flow 图中的回边实现：Flow 的控制图保持无环。每完成、跳过或阻塞一个指纹，N6 从同一 `repair_ledger_index` 读取下一条唯一待处理项，再启动同一 Flow A 内 N7/N8 的下一次执行。全部应用账本均为终态后，才开放批次 Push Gate 并产出最终 `batch_completion_report`。这保留原有“每次只处理一个当前项”的安全语义，同时只维护一份 Flow 定义。

## 7. 状态、重试与可观测性

每应用统一使用：`READY`、`COLLECTING`、`CLOSED`、`NO_EVIDENCE`、`INVESTIGATING`、`EVIDENCE_READY`、`PUBLISHED`、`FAILED`、`NOT_STARTED_DEPENDENCY_FAILED`。每个转换都附带 Artifact hash、Task identity、尝试次数和脱敏失败摘要。

- 对同一 `batch_id + app_key + phase` 的重试必须复用幂等键，并读取已有产物后续办；不得静默重开新的同名文档或重新编号指纹。
- ES/SkyWalking 暂时不可用可重试；闭合失败、映射缺失、文档回读失败必须失败可见，不可降级为成功。
- 仅在 `PUBLISHED` 或 `NO_EVIDENCE` 且回读通过的应用可写“已收集”。批次最终报告必须同时报告这些成功项及失败/阻塞项。
- FlowRun 运行时选择一个 `READY` Environment Version；Flow 模板不保存环境版本。能力与 Context 版本、Secret Reference、输入 Artifact 和环境版本都由 FlowRun Snapshot 冻结。

## 8. 实施顺序与验收标准

1. 将现有规则拆为第 3 节的 Skill/Context，清除其中任何明文连接配置，导入后校验 Capability Version/digest。
2. 先建立 N1–N5 节点资产，固定字段 key；创建并校验 Flow A 的控制边和端口映射。
3. 在测试表和有限应用清单上运行 Flow A：验证多应用隔离、双轨闭合、一个应用失败不影响其他应用、文档模板回读和精确状态回写。
4. 再将 N6–N8 接入同一个 Flow A，逐项验证历史跳过排除、历史已完成询问、方案确认、提交归并与 push Gate 不可绕过。
5. 最后以真实应用表小批量演练；只在每项证据、Artifact、Task 结果和 FlowRun 运行状态可回读时，扩大到整张表。

验收不以“流程显示完成”为准。Flow A 必须能给出逐应用闭合等式、指纹数、调查状态、证据文档链接和失败清单；其 N6–N8 阶段必须证明在用户确认前没有读取待选择项代码、没有代码修改、没有提交、归并或 push。
