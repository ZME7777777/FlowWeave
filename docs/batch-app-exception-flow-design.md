# 多应用异常收集、分析与修复 Flow 设计

状态：DRAFT，供流程闭环评审。

范围：将现有行情异常处理能力编排为一个 FlowWeave Flow A。复用已有 ES、SkyWalking、飞书、Git 和修复 Skill；只为批量执行补充自动模式，不重造同义 Skill。

## 1. 总体结论

采用一个 Flow A、三个节点和一张批次总任务表。

1. 节点一批量收集异常：从飞书应用清理表取得所有应用，对每个应用完成 ERROR 与 Exception 双轨日志收集、精确计数闭合和指纹初分组。
2. 节点二批量分析异常：对所有已闭合指纹补齐 reqId、access、上下游、SkyWalking、同簇或归档证据，生成并回读飞书证据文档。
3. 节点三批量修复异常：依据异常文档，按应用拉取相关 Git 代码、分析因果链、修复、验证、提交及本地合并，并把结果写回总任务表和应用修复账本。

不新增日志收集、链路查询、代码拉取或修复 Skill。三个节点只编排已有能力：

| 现有能力 | Flow A 中的作用 | 需要补充的自动化能力 |
|---|---|---|
| collect-app-exception-logs | N1 和 N2 的顶层业务规则 | 支持 batch、collect、investigate 模式 |
| es-query | N1 的双轨 ES 查询和精确闭合 | 支持输出机器可读的 Markdown 或 JSON 文件 |
| skywalking-query | N2 的链路与 span 调查 | 支持按指纹文件批量输入、逐指纹结果输出 |
| find-and-pull-hq-git | N3 的仓库定位与同步 | 唯一候选时自动 fetch 或首次 clone |
| fix-app-exceptions | N3 的分析、修复、验证和 Git 收尾 | 支持按总任务表批量队列执行 |
| lark-sheets、lark-wiki、lark-doc | 读取应用表、写入和回读证据 | 保持既有文档树、模板和状态写回 |

ES 负责异常全集与应用内部 access 路径；SkyWalking 负责 trace 与 span 证据；Git 代码用于修复。它们互相补充，不能以其中一个缺失来伪造另一个的结论。

## 2. 保持不变的业务规则

每个应用仍是独立处理单元，不能把不同应用的异常混为一组。指纹仍由窗口、环境、应用、logger、endpoint 或任务、root message 和首个业务栈帧组成。

- N1 必须执行 ERROR 和 Exception 双轨检索，并按 ES id 去重；必须证明 logger bucket sum 加 sum other 等于 total，classified 加 remainder 等于 total 且 remainder 为零。
- N2 必须为每个指纹执行代表时间和相邻分钟、应用 host 下候选 reqId、reqId access 链、上下游、SkyWalking ERROR 和 ALL、同簇或归档调查。证据耗尽必须是明确结论，不能由空结果直接推断无异常。
- 飞书只保存阶段一和二事实：窗口父文档与一至七章指纹文档。修复过程、代码、测试、提交和分支只写本地 Markdown 账本。
- 历史已跳过只影响同应用修复队列，不影响日志全集、计数闭合或飞书证据。历史已完成命中记录为复现，不能跨应用自动继承。
- 修复优先保持调用方可观察结果，而非最少改动行数。共享方法、DTO 或 Provider 的改动必须审计调用方，并同时验证故障消除和原业务语义保持。

## 3. 节点设计

### N1 批量收集异常

节点资产名称：批量应用异常收集。

绑定能力：collect-app-exception-logs 的 collect 模式，内部调用 es-query、应用索引和飞书表读取能力。

输入：

| field key | 类型 | 内容 |
|---|---|---|
| application_sheet | URL | 应用异常清理表或 Wiki 链接 |
| batch_options | FILE | 环境、开始结束时间、应用范围、最大并发数和自动化策略 |

输出：

| field key | 类型 | 内容 |
|---|---|---|
| batch_task_table | FILE | 批次总任务表 Markdown，含全部应用和指纹初始状态 |
| collection_report | FILE | 每应用双轨查询、计数闭合、代表日志和失败汇总 |

执行：读取表内所有目标应用及既有 Wiki 链接，使用 app-index 映射 hostname。对每个应用调用现有 ES Skill 的双轨查询能力，完成计数闭合和指纹化。两轨均无结果的应用记录为 NO_EVIDENCE；映射、权限或闭合失败记录为 FAILED 并继续其他应用。

N1 不写飞书证据文档、不访问代码仓库、不进行 SkyWalking 查询。它只产出可复跑的异常全集和总任务表初稿。

### N2 批量分析异常并写飞书证据

节点资产名称：批量异常分析与证据发布。

绑定能力：collect-app-exception-logs 的 investigate 模式，内部调用 es-query、skywalking-query、lark-wiki、lark-doc、lark-sheets 和既有 document templates。

输入：

| field key | 类型 | 内容 |
|---|---|---|
| batch_task_table | FILE | N1 输出的总任务表 |
| collection_report | FILE | N1 输出的日志全集与指纹信息 |

输出：

| field key | 类型 | 内容 |
|---|---|---|
| analyzed_task_table | FILE | 已补充证据链接、调查状态与修复候选状态的总任务表 |
| exception_evidence_index | FILE | 每应用窗口父文档和每指纹文档的飞书 URL 清单 |

执行：只处理 N1 已闭合且确有异常的应用和指纹。沿用既有调查顺序完成 reqId、access、上下游、SkyWalking、同簇或归档证据；按既有模板写入或更新仓库到应用到窗口到指纹的飞书文档，然后逐篇回读。仅全部回读成功的应用才更新清理表为已收集；无异常继续使用未查询到异常日志证据的既有措辞。

N2 不读业务代码，也不写修复状态到飞书。它将可修复项写入总任务表，键为 application slash fingerprint id，例如 hq-interface slash F001。

### N3 批量代码修复

节点资产名称：批量应用异常修复。

绑定能力：fix-app-exceptions 的 batch 模式，内部调用 find-and-pull-hq-git 和既有历史终态过滤、批量跳过、分支和验证脚本。

输入：

| field key | 类型 | 内容 |
|---|---|---|
| analyzed_task_table | FILE | N2 输出的总任务表 |
| exception_evidence_index | FILE | N2 输出的异常文档链接 |
| code_workspace | URL | 用户提供的代码工作空间位置 |

输出：

| field key | 类型 | 内容 |
|---|---|---|
| repaired_task_table | FILE | 最终批次总任务表，含每项修复、验证和 Git 状态 |
| repair_summary | FILE | 按应用和仓库汇总的修复、未修复、阻塞和待发布信息 |

执行：对总任务表中待修复项按应用分组、逐应用串行处理。先为每个应用创建或续办原有 tmp 下的应用修复账本，再用既有历史跳过和历史已完成逻辑过滤，保持跨应用隔离。随后由 find-and-pull-hq-git 定位仓库，fix-app-exceptions 读取一至七章证据、构建因果链和调用方矩阵、选择结果保真修复层、修改代码、执行定向验证并更新应用账本。

N3 可以自动完成代码拉取、分支准备、分析、代码修改、测试、创建本地 commit 和 ff-only 合并到本批次基准分支；任务分支永不 push。默认不 push、不部署、不修改生产配置、不执行破坏性数据库操作。若 batch options 显式开启 push，仍只推送已经验证且含本批次提交的基准分支；部署始终不属于本 Flow。

遇到安全、权限、生产配置、Schema 迁移、不可保持的消费者语义、合并冲突或验证失败时，该项为 BLOCKED 并继续其他应用。节点不能用成功空结果覆盖失败，也不能删除用户已有 Git 改动。

## 4. 批次总任务表

批次总任务表是三个节点间唯一共享事实，文件位于当前 FlowRun 的持久项目目录，并以 FILE Artifact 版本在端口间流转。建议命名为 batch-exception-task.md。它是跨应用调度与可视化总览，不替代现有每应用 tmp 修复账本中的完整因果链、方案和 Git 审计。

| 列 | N1 写入 | N2 补充 | N3 补充 |
|---|---|---|---|
| 批次、应用、仓库、环境、时间窗 | 是 | 只读 | 只读 |
| hostname、表行和 Wiki 节点 | 是 | 校验 | 只读 |
| 指纹 ID、logger、endpoint、root message、首业务栈 | 是 | 校验 | 只读 |
| 收集状态与闭合等式 | 是 | 只读 | 只读 |
| 调查状态、飞书父文档和子文档链接 | 否 | 是 | 只读 |
| 历史终态匹配、应用账本路径 | 否 | 否 | 是 |
| 根因、方案、验证、commit、基准分支、push 状态 | 否 | 否 | 是 |

应用状态为 READY、COLLECTED、NO_EVIDENCE、ANALYZED、REPAIRING、COMPLETED、FAILED 或 BLOCKED。指纹状态为 DISCOVERED、EVIDENCE_READY、SKIPPED_HISTORY、FIXED、NO_CODE_CHANGE、BLOCKED 或 FAILED。任何变更附带来源节点、时间、Artifact 版本和链接，且不得重排已有指纹 ID。

## 5. Flow A 编排

Flow 名称：批量应用异常收集、分析与修复。

运行输入为 application_sheet URL 和 batch_options.md。

N1 批量收集异常输出 batch_task_table.md 与 collection_report.md。

N2 批量分析异常并写飞书证据，输入 N1 两个文件，输出 analyzed_task_table.md 与 exception_evidence_index.md。

N3 批量代码修复，输入 N2 两个文件和运行时提供的 code_workspace URL，输出 repaired_task_table.md 与 repair_summary.md。

控制边为 N1 到 N2 到 N3。端口映射为：N1 的 batch_task_table 映射到 N2 同名输入，N1 的 collection_report 映射到 N2；N2 的 analyzed_task_table 和 exception_evidence_index 分别映射到 N3。code_workspace 是 N3 运行时 URL 输入，不由前两个节点伪造。

N1 与 N2 可在一个自动运行记录中连续执行。N3 读取 N2 产物后处理全部应用，不需要为每个应用动态创建 Flow 节点或另建 FlowRun；应用内逐指纹循环由已有 fix-app-exceptions 队列语义实现。NodeRun 只记录节点级事实，应用和指纹级细节由总任务表及应用账本表达。

## 6. 现有 Skill 的自动化改造

collect-app-exception-logs 增加 mode 参数，取值为 collect 或 investigate；增加 scope 参数，取值为 single-app 或 batch。batch 模式读取和更新总任务表；collect 只做原 Phase 1；investigate 只做原 Phase 2、飞书写入和回读。原单应用交互入口保持兼容。

es-query 增加 machine-readable 输出选项，保留现有人工可读输出。结构化结果必须包含应用、hostname、查询窗口、索引、ES id、计数、去重关系、闭合等式、代表样本和失败原因，且不输出 Secret。

skywalking-query 增加接收指纹文件和输出指纹结果文件的批量入口。查询顺序、环境限制和证据边界不变。

fix-app-exceptions 增加 batch-auto 模式：接受总任务表和证据索引，按应用和指纹处理；把原逐步人工问答改为 batch options 中的明确策略。默认 auto-repair、auto-commit、auto-merge 和 push-enabled 全部为 false。原对话模式仍保留人工处理、跳过和方案确认入口。

find-and-pull-hq-git 增加 auto-resolve 模式：仅对总表中已给出完整应用或仓库且匹配唯一候选的记录自动执行；零或多个候选仍为 BLOCKED，不能猜仓库。

## 7. 自动化策略与安全边界

batch_options 文件明确声明 auto-repair、auto-commit、auto-merge、push-enabled 和最大并发数。未声明时全部默认为 false，最大并发为一。用户可选择只自动到 N2，或开启 N3 的各阶段。

不论策略如何，以下动作永远不属于本 Flow：生产部署、强推、reset hard、clean force、删除远端分支、数据库删除、未经明确白名单的 Schema 迁移，以及写入明文凭据。ES、SkyWalking、飞书和 Git 认证均通过 FlowWeave 管理的连接或 Secret Reference 注入，不能进入 Skill 文件、任务表、Artifact 或日志。

## 8. 分步实施与验收

第一步只实现 N1：用少量应用验证应用表解析、双轨闭合、总任务表和失败隔离。第二步接入 N2：验证 SkyWalking 取证、飞书模板回读和已收集状态写回。第三步接入 N3：用非生产仓库或小批量应用验证历史终态隔离、自动修复、测试、commit 和本地合并策略。

Flow A 的最低验收条件：每个应用都有可读状态；每个有异常的应用都有完整闭合等式和飞书证据链接；每个修复项都可追溯到应用 slash 指纹、应用账本、代码分支、验证结果和 commit；失败或阻塞应用不会阻止其他应用完成，也不会被伪报为无异常或已修复。
