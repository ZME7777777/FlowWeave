# FlowWeave OpenHands-first 重构任务清单

> 本文件是可恢复的持久化执行状态。恢复工作时必须完整读取 `openhands-agent-server-design.md`、`openhands-capability-enhancement-roadmap.md`、本文件、审计文档、Goal（若有）、`git status`、相关 diff、Alembic heads 和最近验证结果。任一时刻只允许一个顶层任务为 `IN_PROGRESS`、一个当前执行批次；批次由一个或多个 `CURRENT` 原子任务组成。T1-T8 只做基本代码健康检查；行为、集成、恢复、真实 Runtime 和 E2E 功能验收统一在 T9 执行。

## 状态

- 唯一总目标：按照 OpenHands-first 和产物驱动运行原则重构 FlowWeave，快速修正错误、冗余和重复执行实现；FlowWeave 只作为不可变能力治理、流程/资源/审批/审计和 Artifact 投影控制面，由 OpenHands 原生执行 Agent 能力。
- 当前主任务：T8 产品 API、UI 与文档收口。
- 当前执行批次：`T8.01–T8.09 产品 API、UI 与文档收口`
- 最近验证结果：T7.16–T7.19 已冻结 Hook Set、Critic、Goal 与 `ask_agent` 正式契约并完成耐久治理；T7.20–T7.32 已逐项 `DECIDED_NO`。T7.33 已把固定四包 1.42.0、source commit/ref、正式 HTTP operation、`StartConversationRequest` 字段、Server capability 结构和 Snapshot 实际 Tool 集编入不可变 Runtime contract；每次 Conversation 创建前经 `/ready`、`/server_info`、`/openapi.json` 重新协商，不兼容时在任何创建副作用前 fail closed。改动文件 Ruff/compile、窄范围 Pyright、OpenAPI 97 paths 基线、0047 唯一迁移 head、固定 1.42.0 镜像契约（镜像 `sha256:059cbae5eeec46be007f5b477cc8c8af018e684ac719a16678adda1b800c3bef`）、六项最小拒绝路径与 `git diff --check` 通过；行为、恢复竞态、预算精度和真实 Runtime 验收留到 T9。
- 工作区基线：2026-08-14 当前为 `master`，已有连续重构改动；当前 Alembic head 为 `0047_runtime_agent_governance`。

## 顶层任务

### 状态与门禁规则

- `PENDING`：尚未开始；`IN_PROGRESS`：当前唯一执行任务；`PAUSED`：有可复现阻塞或被更高优先级主链暂停；`IMPLEMENTED`：代码实现和基本检查通过、等待 T9 功能验收；`COMPLETE`：T9 最终功能门禁已经通过。
- 原子任务状态：`DONE` 表示代码已落地并通过基本代码门禁，不代表功能验收完成，且 T9 前不得重跑；`CURRENT` 表示属于当前执行批次，同一批次可有多个；`READY` 表示前置条件满足后可加入批次；`SKIP` 表示用户无条件跳过；`UPSTREAM_BLOCKED` 表示正式能力缺失且已有安全降级和解锁条件；`DECIDED_NO` 只允许用于非产品必做接口，并必须有逐项理由。
- 用户可选择单项、列表、连续范围或同一顶层任务内整组任务作为当前执行批次。批次内按依赖顺序连续完成，全部处理完才交接；不得实施批次外的 `READY` 任务。
- 第 11 章产品必做域不得标为 `DECIDED_NO`：Confirmation、长会话上下文、MCP、费用/可观测性、实时事件、Conversation 分支、Browser、Tool Policy、原生子 Agent、Skills/Plugins/Marketplace、Agent/LLM Profile、ACP、直接 Bash/File/Git/Workspace/Trajectory API。
- 每个 T1-T8 切片的基本代码门禁只包含：改动文件格式/Lint或语法检查、受影响文件/包的最窄 typecheck/compile、API schema 可生成、新迁移可导入且升级到唯一 head，以及 `git diff --check`。
- T1-T8 不运行行为单元测试、集成测试、恢复/竞态矩阵、真实 Runtime、容器 smoke、E2E、平台全量测试、完整 Web build 或历史迁移矩阵；统一留到 T9。安全/fail-closed 改动只补最小拒绝证明；新增 OpenHands 契约假设只补固定源码或最小镜像探针，二者都不得扩张成功能验收。
- 原子任务正文中的“验收”“覆盖”“验证”条件默认属于 T9 功能验收清单；普通切片只实现对应代码并通过基本代码门禁。
- 完成一个顶层任务的全部实现门禁后标记 `IMPLEMENTED`，不标记 `COMPLETE`。T9 集中执行最终验证矩阵并统一晋级。
- 外部阻塞任务记录证据、安全降级和解锁条件后可保持 `PAUSED`，随后恢复任务清单中最早的可执行任务；任一时刻仍只有一个 `IN_PROGRESS`。

### 第 11 章覆盖账本

本表是 T9 逐项功能验收索引。`DONE` 仅说明实现已落地，T9 仍须按本表和原子任务正文验证行为；“已归入某个 Phase”不算覆盖。

| 设计条目 | 产品处置 | 已完成原子项 | 剩余原子项 | 当前结论 |
|---|---|---|---|---|
| 11.3 Tool Action 确认 | 必做 | T2 全部切片 | T9 真服务回归 | DONE |
| 11.4 Condenser / Memory | 必做 | T2 Condenser；T6.01–T6.05 | T9 真实挂载/加载 smoke | DONE |
| 11.5 MCP 验证与 OAuth | 必做 | T4 MCP probe、Secret Reference、OAuth job | T4-B01 Tool schema 正式契约 | PARTIAL / UPSTREAM_BLOCKED |
| 11.6 费用与可观测性 | 必做 | T5 子 Agent usage | T6.09–T6.12（SKIP） | PARTIAL / SKIP |
| 11.7 实时事件 | 必做 | REST cursor 补偿；T6.13–T6.14 WS 唤醒与 Bash 补偿 | T9 真实断线/恢复验收 | DONE |
| 11.8 Conversation 分支 | 必做 | T6.06–T6.07 | T6.08（SKIP） | PARTIAL |
| 11.9 Browser / 直接 Bash / IDE / Desktop | 必做 | Tool 可用性契约基础 | T6.17–T6.19、T7.09、T7.14–T7.15（SKIP） | SKIP |
| 11.10 Tool 集与 Tool Policy | 必做 | T3 不可变 Policy；T6.15–T6.16 catalog/分类/确认/并发与正式工具集 | T8.02、T9 真实竞争验收 | DONE（实现） |
| 11.11 原生子 Agent | 必做 | T5 原生 Task 全部已实现切片 | T5-B01–T5-B03 | PARTIAL / UPSTREAM_BLOCKED |
| 11.12 Skills / Plugins / Marketplace | 必做 | T3/T4 固定版本、原生 Loader/激活/invoke | T8.01 | PARTIAL |
| 11.13 Agent/LLM Profile | 必做 | T3 不可变模板和显式物化；T7.01–T7.02 治理/字段矩阵/不可变切换 | T8.03、T9 功能验收 | DONE（实现） |
| 11.14 ACP Agent | 必做 | 无 | T7.03–T7.08（SKIP） | SKIP |
| 11.15 Critic / Goal | 已纳入产品 | Critic Policy 与 Hook Set 冻结；T7.16–T7.18 正式事件、控制、预算、恢复和审计 | T9 功能/恢复/真实 Runtime 验收 | DONE（实现） |
| 11.16 File/Git/Workspace/Trajectory | 必做 | 受管 Workspace/Artifact 基础 | T7.10–T7.13（SKIP） | SKIP |
| 11.17 运行控制、诊断与预热 | 逐项决定 | T7.19 `ask_agent`；T7.20–T7.32 逐项 `DECIDED_NO`；T7.33 Runtime 能力协商 | T9 | DONE（实现） |

### T1 基线审计与目标 Schema 设计 — IMPLEMENTED

范围：

- [x] 读取用户任务、仓库约束并检查 Git 状态。
- [x] 完整阅读两份设计文档并提取分阶段验收项。
- [x] 审计 Capability、Conversation、Runtime、Sandbox、Task、迁移和 Web UI。
- [x] 从固定 OpenHands 1.40.0 安装产物冻结路由、字段、Tool、事件和默认值契约。
- [x] 将能力标记为已实现、部分实现、重复实现或缺失。
- [x] 设计最终模块边界、RuntimePort、耐久投影边界和数据库 Schema。
- [x] 明确旧表、字段、兼容逻辑和重复实现的删除门槛。

退出条件：审计证据可复现；目标 Schema 与模块边界形成文档；OpenHands 1.40.0 契约基线有自动测试；首个 Phase 0 改动已确定且不依赖猜测。

### T2 Phase 0：协议正确性 — IMPLEMENTED

范围：OpenHands 原生 Confirmation 批次审批；pending action batch digest、防漂移、幂等、审计；RuntimePort 与事件模型；Condenser 配置和事件投影；WebSocket 瞬时事件与耐久 REST cursor 边界；1.40.0 版本契约测试。

已完成切片：

- [x] 按 OpenHands 1.40.0 原生批次语义提取 pending actions，计算稳定 digest，并只允许整批批准或拒绝。
- [x] 新增耐久 `RuntimeConfirmationBatch` 投影、状态约束、唯一活跃批次、决策幂等键、Attempt/批次双版本 fencing 与启动恢复任务。
- [x] 原生确认从普通 human-input/resume 路径分离；批准调用正式确认端点，拒绝写入原生拒绝 Observation 后通过正式 `/run` 续跑。
- [x] 修复 Worker 在 Runtime 已生效但数据库未回写时的重放窗口；仅 `idle/paused` 可触发 `/run`，终态不重新执行。
- [x] 节点配置冻结 `ALWAYS|NEVER` Confirmation policy，旧 Snapshot fail closed 到 `ALWAYS`，并映射为 OpenHands `AlwaysConfirm|NeverConfirm`。
- [x] Workbench 与 Agent Chat 展示同一耐久批次，明确不支持伪逐 Action 审批，并在等待确认时禁用普通消息替代审批。
- [x] 增加正式批次决策 API、前端类型和 OpenAPI 基线。
- [x] Condenser 配置冻结、原生事件投影、手动命令、租约 fencing、崩溃重放和审计。
- [x] WebSocket 仅作瞬时文本通道；REST cursor 在通知缺失和 anchor 暂时不可见时补偿且不重放旧 Finish。
- [x] 固定 1.40.0 镜像契约，以及真实 create → Tool Confirmation → approve → Finish 和 Condenser E2E。

退出条件：路线图 Phase 0 全部验收项通过；普通 resume 不再处理原生确认；批次漂移和重试测试通过；Condenser 可冻结、运行、恢复和审计；实时通知丢失可由 cursor 补偿。

### T3 Phase 1：统一能力仓库 — IMPLEMENTED

范围：Capability Package、Version、Blob、Dependency、Validation；Skill、Plugin、MCP、Hook、Agent Definition、Tool/Context/Memory/Critic Policy；Agent Profile；RuntimeAgentSpec；Snapshot Runtime Manifest；移除 `CapabilityImport` 永久版本语义和旧类型限制；按最终设计重建迁移基线与测试库。

已完成核心切片：

- [x] 新增不可变 Capability Package、Version、Blob、Dependency 与 Validation 仓库；Import 仅保留一次性发布和来源审计语义。
- [x] Skill、MCP、Hook 列表、节点绑定和 Skill Collection 统一使用 Version UUID；应用层不再生成 `import_id:position` 永久身份。
- [x] Skill 编辑发布新 Version，不修改旧 Blob/Version、旧节点或旧工作区；删除改为引用保护后的逻辑退役。
- [x] 依赖构建发布同 Package/Blob 下的派生 READY Version，不原地修改源 Version 或重绑既有消费者。
- [x] Run Snapshot 冻结 Version UUID、digest、Blob hash 和运行配置；Runtime 只消费并校验 Snapshot Runtime Manifest，篡改时 fail closed。
- [x] 0029–0030 支持空库与真实 0028 历史数据升级；旧节点引用和旧 Snapshot 确定性回填到同一 Version/digest/Blob。
- [x] OpenAPI 与现有前端能力 ID 契约切换为规范 UUID。
- [x] Tool Policy 作为不可变 Capability Version 冻结；每节点恰好绑定一个策略，RuntimeAgentSpec 只从 Snapshot Manifest 编译，OpenHands Adapter 不再补隐式默认 Tool。
- [x] Agent Definition 作为不可变 Capability Version 发布、绑定并冻结；主 Tool Policy 必须显式允许 `task_tool_set`，子定义 Tool 必须是父策略子集；OpenHands create 请求使用原生 `agent_definitions`，平台私有委派提示词和 JSON 解析入口已删除。
- [x] Plugin 作为不可变 Capability Version 发布、绑定并独立冻结到 RuntimeAgentSpec；导入时解析 manifest、贡献和逐文件摘要，Attempt 只物化对象存储中的固定内容并通过 OpenHands 原生 `plugins` 创建字段加载。
- [x] Plugin 生产旁路已 fail closed：禁用 OpenHands 用户/项目 ambient Plugin 发现，不暴露运行中 `load_plugin`；对象整体摘要、逐文件摘要、路径、文件类型和只读挂载均在启动前复核。
- [x] Agent Profile 作为无 Secret 的不可变 Capability Version 导入；Profile 引用的 Tool/Context/Memory/Critic Policy 均解析为固定 Version UUID，节点绑定与 executor 设置不一致时拒绝发布。
- [x] Snapshot 冻结 Profile version/digest/content hash 并完全物化到显式 RuntimeAgentSpec；RuntimePort 保留 Profile provenance，OpenHands 请求只传显式 `agent` 并把 Profile identity 写入原生 observability metadata，禁止通过 `agent_profile_id` 或 `agent_settings` 读取可变 Server Profile Store。

源码运行时适配主链：

- [x] 建立 source-built bootstrap：`source.lock.json` 冻结 upstream v1.40.0 commit、归档 URL 与
  SHA-256；Docker 校验归档后从固定源码覆盖安装四个 OpenHands 包，不读取浮动 branch、共享
  工作树或隐式本地源码。镜像保存源码/overlay provenance，构建门禁额外输出不可自引用写入镜像的
  image ID。
- [x] 契约探针校验 bootstrap provenance、四包 `direct_url.json` 源码路径、1.40.0 版本与现有兼容
  契约；下载器对浮动 URL、错误摘要和 tar 路径穿越 fail closed。
- [x] 将 source lock 从历史 v1.40.0 bootstrap 切换到 OpenHands 固定目标提交
  `f09e03eac772290feeb51b7d7390ffaefeca1a09`，冻结归档 digest、包版本和源码 provenance；不得读取
  浮动 `main` 或修改 OpenHands 工作树。
- [x] 明确产品驱动的适配边界：不做 OpenHands 能力覆盖率或 v1.40.0 → 目标源码的全量差异盘点；
  后续仅在 FlowWeave 具体产品切片需要时，对固定目标源码和实际镜像做最小契约取证。
- [x] 已知但尚无产品执行闭环的能力保持 fail closed：启用 Memory 在节点绑定阶段以
  `MEMORY_SOURCE_UNAVAILABLE` 拒绝；未来 OpenHands 二开必须另立任务并获得明确授权。
- [x] Context/Critic Policy 自定义不可变 Version 可由节点绑定冻结进 Snapshot，并编译到
  `StartAttemptRequest` 的原生 AgentContext/Critic 配置；相关版本身份和运行参数有定向回归。

后续产品范围归属：Memory 内容治理、作用域、保留与物化属于 T6；Critic 的产品治理、事件、恢复和成本闭环以及 Profile UI 属于 T7；Marketplace/Git 固定来源、MCP/Plugin 目标环境验证和 Secret Reference 属于 T4。它们不再阻塞 T3。

实现门禁：所有运行能力解析为固定 version/digest；Snapshot manifest 不可变；Secret 仅以引用存在；旧永久 Import/Skill-only 集合语义删除。每个切片只运行相关能力/Manifest 定向测试；新增迁移时立即证明新 head 可加载和本次升级路径，历史全链留到 T9。全部实现完成后标记 `IMPLEMENTED`。

T3 实现门禁已通过，等待 T9 的集中全量验证后再由 `IMPLEMENTED` 晋级 `COMPLETE`。

### T4 Phase 2：MCP、Plugin 和能力市场 — IMPLEMENTED

范围：目标环境 MCP probe、Tool Catalog/Schema/只读试调用；OAuth Secret Reference 的刷新、撤销和审计；Marketplace/Git 固定 commit/digest 导入；OpenHands 原生 Plugin 加载；Skill 原生触发和 invoke。

已完成子切片：

- [x] 本地 Plugin ZIP 作为不可变 Capability Version 发布；严格解析 OpenHands 1.40.0 实际合并的 Skill、Command、MCP 与 Hook，拒绝无有效贡献、无效内嵌配置和 1.40.0 不会合并的 Plugin `agents/`。
- [x] Plugin Version 独立冻结到 Snapshot RuntimeAgentSpec；对象整体 hash、逐文件 hash、路径和普通文件类型在启动前复核，并物化到 Runtime 独立只读挂载。
- [x] OpenHands create 请求仅传冻结的本地 `PluginSource`，同时显式设置 `load_ambient_plugins=false`，阻断用户目录、项目目录和 Marketplace 安装态旁路。
- [x] 固定 OpenHands 1.40.0 镜像探针冻结 `PluginSource(source, ref, repo_path)` 与 create 顶层 `plugins` / `load_ambient_plugins` 契约。
- [x] Git Plugin 仅接受允许域名上的无凭据 HTTPS URL、完整 40 位 commit 与安全 `repo_path`；Worker 在事务外通过固定 OpenHands 1.40.0 resolver 解析并二次校验规范 ZIP，READY 后由用户显式发布不可变 Capability Version。控制面提供有界轮询、贡献预览、失败/过期重试；Runtime 仍只接收本地只读 `PluginSource`，远端 URL 仅保留在来源审计。
- [x] MCP Capability Version 可在指定 READY Environment Version 的短生命周期受管 Runtime 中调用 OpenHands 正式 `POST /api/mcp/test`；0038 以 `CapabilityValidation` 投影 RUNNING/PASSED/FAILED、目标环境、Tool 名称目录和完成时间，异常也耐久失败并请求回收 Sandbox。
- [x] 可选只读试调用只保存 `is_error`、结果字节数与 SHA-256，不保存目标系统正文；OAuth state 在 Secret Reference 生命周期实现前以 `MCP_OAUTH_LIFECYCLE_REQUIRED` fail closed。环境版本被验证审计引用时禁止删除。
- [x] 固定 1.42.0 镜像探针确认 MCP test 请求含 `name/server/timeout/tool_call`，成功/失败响应为正式判别联合；正式成功响应只暴露 Tool 名称而不暴露 input schema，因此 Schema 明确投影为 `UNAVAILABLE_FROM_OPENHANDS_MCP_TEST`，不在 FlowWeave 伪造私有 schema 协议。
- [x] 0039 新增按 MCP Capability Version 与 Environment Version 唯一绑定的 OAuth Secret Reference；OpenHands 正式 `server.auth.state` 仅在调用边界解密，顶层 `oauth_state` 返回后立即以平台主密钥整体加密，普通 Capability、Snapshot、验证报告、API 和审计不保存或返回 token/client secret。
- [x] OAuth state 刷新使用 Secret `state_version` CAS；并发刷新或撤销后旧 probe 不得覆盖或复活凭据。撤销原子清空密文和摘要，后续 probe fail closed；审计只保存引用、版本、动作、验证 ID 与 state 摘要。规范 JSON、256 KiB 上限、有限 expiry、密文解密/结构/摘要完整性均有拒绝路径。
- [x] Capability 导入拒绝内嵌 `auth.state`；Secret Reference 即使撤销也保留目标环境审计身份并显式阻止 Environment Version/Environment 删除。固定 1.42.0 镜像探针执行验证正式输入位于 `server.auth.state`、输出位于成功响应顶层 `oauth_state`。
- [x] 0040 新增耐久 MCP OAuth Authorization，固定 Secret Reference/Capability Version/Environment Version、预期 Secret 版本、专属受管 Runtime 与 OpenHands 原生 job ID；正式 `start/status/callback` 路由完成首次浏览器授权，授权 URL 加密暂存且终态清除，callback URL 不落库，最终 OAuth state 只经 Secret CAS 整体加密写回。
- [x] 授权状态机以 Authorization 版本和 Secret 版本双重 fencing；并发 status/callback、撤销、过期或 Secret 漂移不能覆盖或复活凭据。终态、过期与撤销仅在数据库提交后发出 Runtime/Workspace 回收，回滚不会提前删除仍有效的 Runtime；Sandbox 协调器和远程控制器正式识别 `MCP_OAUTH_AUTHORIZATION` owner。
- [x] 已发布 Git/本地 Plugin Capability Version 可在指定 READY Environment Version 的短生命周期受管 Runtime 中验证；平台复用 `CapabilityValidation` 和 `CAPABILITY_VALIDATION` Sandbox owner，先复核冻结 ZIP/逐文件摘要并只读物化，再由目标 1.42.0 镜像正式 `openhands.sdk.plugin.Plugin.load` 解析。验证报告只保存 manifest 名称、版本和 Skill/Command/Agent/MCP/Hook 数量，不保存 Plugin 正文；失败状态耐久化并请求回收 Runtime/Workspace。
- [x] Plugin Loader 控制面仅暴露固定高层操作，不接受命令、代码、环境变量或挂载；API/Worker 远程模式经认证 Docker Controller，本地模式复用同一共享实现。路由与共享执行层均校验 validation 专属 `/runtime/capabilities/nodes/plugin-probe-<id>/plugins/<name>` 只读路径，并在执行前校验受管 Sandbox ownership。
- [x] 0041 新增固定 Marketplace Plugin 来源治理：调用方提交 Marketplace 的无凭据 HTTPS URL、完整目录 commit、安全 repo path 与条目名；隔离 resolver 使用目标 1.42.0 正式 `MarketplaceRegistration` / `MarketplaceRegistry` 解析相对或外部 Plugin source，并冻结实际 Plugin commit。控制面再次校验 allowlist、完整 SHA 与安全子路径，耐久保存目录快照和实际 Plugin 双层 provenance；发布后的 Runtime 配置只含本地不可变对象，不含远程 URL、ref 或 Marketplace 注册。
- [x] 本切片不启用 Agent Server 的浮动 Marketplace catalog cache、安装态、auto-load 或运行中 attach；Docker Controller 仅开放固定结构化解析路由并按 worker principal fail closed。OpenAPI 已提供固定条目选择入口；完整目录浏览 UI 留给 T8。
- [x] 冻结 Skill 以正式 AgentSkills 格式进入 `AgentContext`；每个治理后的 Skill 使用稳定 `$<capability_key>` 作为 OpenHands `KeywordTrigger`，结构化选择只补该触发词且不重复用户已有触发词，不再生成“先读取并遵循 Skill”的平台指令。
- [x] OpenHands 为可调用 AgentSkills 条件挂载原生 `InvokeSkillTool`；FlowWeave 不新增 Tool Action HTTP 或私有控制协议，并移除系统上下文直接披露 Skill 正文、目录和脚本的旁路。
- [x] Runtime 事件投影保留原生 `MessageEvent.activated_skills`、`InvokeSkillAction` / `InvokeSkillObservation` 及其 `id`、`action_id`、`tool_call_id`、`llm_response_id`；固定 1.42.0 镜像实际执行关键词激活、显式 invoke 和 `invoked_skills` 去重。

后续归属：Marketplace 目录浏览 UI 移交 T8；固定 1.42.0 的正式 MCP test 不提供 Tool schema，未来只有正式接口出现后才补投影，不在 FlowWeave 伪造。二者不阻塞 T4 实现门禁。

T4 实现门禁已通过，等待 T9 的集中全量验证后再由 `IMPLEMENTED` 晋级 `COMPLETE`。

实现门禁：浮动来源不能进入运行时；目标环境验证和 Secret Reference 边界有对应实现；Plugin/Skill 只走 OpenHands 原生 Loader。每个切片运行对应 resolver/probe/泄漏拒绝定向测试；真实目标环境的最小新增契约当场验证，完整容器矩阵留到 T9。全部实现完成后标记 `IMPLEMENTED`。

正式上游阻塞：

- **T4-B01 MCP Tool input schema — UPSTREAM_BLOCKED**：固定 1.42.0 的正式 `/api/mcp/test` 成功响应只返回 Tool 名称，不返回 input schema。FlowWeave 已将 schema 明确投影为 `UNAVAILABLE_FROM_OPENHANDS_MCP_TEST`，禁止抓取私有内部状态或自建协议。解锁条件：固定目标 OpenHands 出现正式 schema 字段/API，或用户明确授权独立 OpenHands fork 后，恢复为普通原子切片并补迁移、API、UI 和兼容测试。

### T5 Phase 3：OpenHands 原生子 Agent — IMPLEMENTED

范围：Agent Definition；Task Tool / `task_tool_set`；子 Agent 生命周期、状态、成本、取消、确认和预算投影；必要的 OpenHands 正式事件/API 及上游测试；删除 Finish 控制 JSON、`_delegation_tasks` 和平台重复执行。

已完成子切片：

- [x] 冻结并校验 Agent Definition 与 `task_tool_set`，通过 OpenHands 原生创建请求注册定义；子定义 Tool 必须是父 Tool Policy 子集且不能递归委派。
- [x] 将 1.40.0 正式 `TaskAction` / `TaskObservation` 归一化为耐久 `RuntimeSubagentTask` 投影；只用事件 `id`、`action_id`、`tool_call_id`、`llm_response_id` 与原生 `task_id` 关联，Observation 先到、重复重放和身份漂移均有测试。
- [x] Task prompt 从 Runtime 事件详情、投影表和 API 中排除；只保存描述、原生状态和已公开的 Observation 结果。
- [x] 子 Agent UI 改为读取原生 Task 投影；`/api/sub-agents` 只被视为 Agent Definition 发现接口，不虚构运行中子会话查询或单 Task 控制能力。
- [x] 删除平台 Finish delegation JSON 执行入口和 `_delegation_tasks` 解析；0036 将旧 `SUBAGENT` 会话冻结为只读、杀死遗留后台任务并回收 Sandbox，同时删除 `parent_conversation_id`、`delegation_batch_key`、`delegation_instruction` 执行协议列。
- [x] 固定 OpenHands 1.40.0 真实 smoke 覆盖父 Agent 原生调用 Task Tool，并校验 `TaskAction → TaskObservation` 的正式关联字段与完成状态。
- [x] 固定 1.40.0 Task 治理契约：Task Tool 在父进程内同步阻塞，状态仅 `running/completed/error`；公开服务只有定义发现 `/api/sub-agents`，没有运行中 Task 列表、单 Task cancel/pause/interrupt 或异步确认 HTTP 身份；`resume` 只依赖当前 `TaskManager` 的进程内注册表，不能在新 Manager 中恢复。
- [x] 固定 1.40.0 Task 成本事实：子 Agent 运行时使用独立 metrics，Task 结束前以累计替换语义写入父 Conversation 的 `stats.usage_to_metrics["task:<task_id>"]`；真实 Agent Server smoke 已用 `TaskObservation.task_id` 验证该正式归因键。
- [x] 子 Agent Confirmation fail closed：固定镜像已执行正式空参数 `Tool(name="task_tool_set") → resolve_tool(...) → TaskToolSet.create(conv_state=...)` registry 链并证明生成的 `TaskManager` 没有 `confirmation_handler`；SDK 对等待确认的子 Action 会自动继续。因此 FlowWeave Agent Definition 当前只接受显式 `permission_mode=never_confirm`，拒绝继承、`always_confirm` 和 `confirm_risky`，防止配置声称需审批而 Runtime 自动批准。
- [x] 从父 Conversation 正式 `stats.usage_to_metrics["task:<task_id>"]` 投影累计 cost/token 快照；只用 `TaskObservation.task_id` 关联，按 digest/version 累计替换，同快照重放幂等、回退快照 fail closed。
- [x] 0037 新增一 Task identity 一账本的 `runtime_subagent_task_usage`；resume 可有多个 Action/Observation invocation，但只保留一个累计账本并指向最新 invocation，避免父子和 resume 双计。
- [x] Agent Definition 冻结的 `max_budget_per_run` 进入耐久预算事实；越界仅记录审计和 UI 告警，明确不伪造 1.40.0 缺失的运行中单 Task 控制动作。
- [x] 子 Agent API/OpenAPI/Web 展示累计 USD、输入/输出 token、预算及越界；不保存 prompt、Secret 或完整 OpenHands metrics history。
- [x] 补齐 AUTO Attempt 的原生 Task 生命周期投影，并在 Task 终态事件与 `task:<task_id>` stats 非原子可见时保持父执行运行、有界重读；事件先到和 stats 先到都只用正式 `task_id` 对账，成功/失败均不会提前闭环。
- [x] usage 恢复默认最多 5 次轮询；恢复中与耗尽均记录脱敏审计，耗尽后以 `RUNTIME_TASK_USAGE_UNAVAILABLE` fail closed，不伪造零 usage、不重复计费，也不引入私有 Task 控制协议。
- [x] 固定 1.40.0 父级取消边界：`ParallelToolExecutor` 取消 async wrapper 后只调用 `TaskExecutor.interrupt()`，而该方法继承默认 no-op；镜像探针直接证明已启动的同步 Task 线程在父 interrupt 后继续存活。
- [x] 共享 Agent Server 若存在未收到 Observation 的 `REQUESTED` Task，父 interrupt 后以 `RUNTIME_TASK_CANCEL_UNCONFIRMED` / `CANCEL_FAILED` fail closed 并审计正式 Action/tool-call identity，不再错误标记 Attempt `CANCELLED`。
- [x] 受管独立 Runtime 的在途 Task 取消只以 Sandbox `observed_state=DELETED` 作为整 Runtime 执行停止事实；删除未完成时耐久重试，完成后记录 `MANAGED_RUNTIME` 作用域审计，不伪造单 Task `CANCELLED` 状态或 Observation。
- [x] `RUNTIME_TASK_CANCEL_UNCONFIRMED` 提供显式恢复模式：共享 Runtime 只能重新读取正式 cursor、投影迟到 `TaskObservation`/累计 stats 并重试父 interrupt；存在受管 Sandbox 时才开放经风险确认的整 Runtime 清理，不暴露虚假的单 Task 控制。
- [x] 取消恢复继承 Task usage 的非原子可见性规则：Observation 已到但 `task:<task_id>` stats 尚未可见时保持 `CANCELLING` 并耐久有界重读，耗尽后以 `RUNTIME_TASK_USAGE_UNAVAILABLE` fail closed。
- [x] 恢复模式写入耐久 `HumanAction` 与后台任务 payload；投递丢失或 Worker 重启时从审计动作重建相同模式和 20 次重试上限，受管清理仍只以 Sandbox 物理删除为完成事实。
- [x] Attempt API 投影当前可用恢复模式，Workbench 分别展示“重新对账”和高风险“清理整个受管 Runtime”；OpenAPI 与前端类型已同步。
- [x] 固定目标 1.42.0 源码与实际镜像完成最终 Task 缺口审计：公开 HTTP 仍只有 `/api/sub-agents` 定义发现；`TaskExecutor.interrupt` 仍是 no-op，已启动同步 Task 不随父 interrupt 停止；正式 registry 创建的 `TaskManager` 仍无 confirmation handler；子会话文件虽进入父持久目录，新 Manager 仍不扫描目录重建 `task_id` 身份。三项缺口继续沿用现有 fail-closed 产品路径，未来若补能力须另立并明确授权 OpenHands 二开任务。

T5 实现门禁已通过，等待 T9 的集中全量验证后再由 `IMPLEMENTED` 晋级 `COMPLETE`。目标源码缺失的单 Task 取消、异步确认和重启 resume 不属于 FlowWeave 平台补协议范围；现有安全降级已由目标镜像负向契约冻结。

实现门禁：FlowWeave 仅治理和投影，旧委派协议及执行器删除；目标源码已有的成功、失败、usage、预算、取消、确认与恢复能力均通过正式契约接入并有定向测试。目标源码未提供的能力保持 fail closed，不在 FlowWeave 中伪造。固定目标源码的真实全链 smoke 留到 T9。

正式上游阻塞：

- **T5-B01 单 Task 取消 — UPSTREAM_BLOCKED**：`TaskExecutor.interrupt()` 仍为 no-op；共享 Runtime 只允许正式事件重新对账，受管 Runtime 只以整个 Sandbox 物理删除证明停止。解锁条件：OpenHands 提供可寻址 Task identity 和正式 cancel/interrupt 生命周期。
- **T5-B02 子 Agent 异步确认 — UPSTREAM_BLOCKED**：正式 registry 创建的 `TaskManager` 没有 confirmation handler，当前 Agent Definition 强制 `never_confirm`。解锁条件：OpenHands 提供能由控制面关联、决策和恢复的 Task Confirmation 契约。
- **T5-B03 子 Agent 重启 resume — UPSTREAM_BLOCKED**：新 `TaskManager` 不从持久目录恢复 `task_id` 索引。解锁条件：OpenHands 提供稳定持久身份和服务重启后的正式 resume 契约。

### T6 产品运行时基础 — IMPLEMENTED

执行顺序固定如下。每项代码完成并通过基本代码门禁后即可推进；正文中的行为/恢复/真实运行条件统一留给 T9，`DONE` 项在 T9 前不得重跑。

#### Memory 与长会话上下文

- **T6.01 Memory 正式契约与 fail-closed 边界 — DONE**：固定 1.42.0 单开关、双 tier 合并和读取失败静默降级已由镜像探针证明。
- **T6.02 Memory Source 不可变内容模型 — DONE**：0042、规范 UTF-8、digest、追加式版本与数据库防篡改已通过。
- **T6.03 Memory 审查、扫描与激活 — DONE**：0043、独立审查、服务端扫描、治理 CAS、唯一 ACTIVE 已通过。
- **T6.04 Memory 保留、过期与删除 — DONE**：0044、冻结保留期、引用保护和不可恢复墓碑已通过。
- **T6.05 Memory 隔离物化与挂载 — DONE**：Snapshot hold、owner/scope/tier 隔离、只读原子物化、受管 Docker 正式路径挂载与生命周期清理已通过。

#### Conversation 分支

- **T6.06 原生 Conversation Fork — DONE**：为同一冻结 Snapshot、兼容 Runtime/Workspace 的分支调用正式 `/fork`，耐久保存 source conversation/event、fork conversation、event tree identity、metrics 处置和幂等键。验收：完整/指定 Event fork、重放、超时后恢复、身份漂移拒绝、旧 Runtime 不可用拒绝均有定向测试；不得序列化文本冒充原生历史。
- **T6.07 Semantic Fork 显式降级 — DONE**：仅在跨 Snapshot、跨 Environment、Runtime 已回收或用户明确选择时创建语义分叉；API/UI/审计必须标明不继承 Tool/Agent state、Skill、Condensation、stats 和 HEAD。验收：不会静默回退，旧 `conversation_history` 高保真声明和隐式文本路径删除。
- **T6.08 Navigate/HEAD 治理 — SKIP**：接入正式 `/navigate`，冻结 expected head/event、权限、互斥租约、审计和恢复；普通协作用户不获得无约束历史改写。验收：并发 HEAD 漂移、越权、不可达 Event、Runtime 已回收和重放均 fail closed。

#### 费用、预算与 Trace

- **T6.09 父 Conversation stats 规范化 — SKIP**：从正式 stats/cursor 读取主模型、Condenser、Critic 与 Task 指标，定义累计替换和稳定来源 identity；不得保存 prompt、Secret 或原始完整 metrics history。
- **T6.10 通用 usage ledger 与层级对账 — SKIP**：建立 Conversation/Attempt/Node Run/Run 累计账本，Task 专用账本只归因一次；覆盖乱序、重复、回退快照、终态非原子可见和重建。
- **T6.11 预算治理与停止语义 — SKIP**：冻结软/硬预算、预警阈值和模型降级策略；只使用 OpenHands 正式 pause/interrupt/cancel 能力，不能保证停止时显式 fail closed，且与子 Agent/Goal 重试不乘算。
- **T6.12 Trace 关联与可观测性脱敏 — SKIP**：注入稳定低基数 Run/Node/Attempt/Conversation/provider metadata、tags 和 span name，并投影可跳转的 trace identity；拒绝消息、Artifact、Secret 和高基数动态正文。

#### 实时事件

- **T6.13 Conversation WebSocket 唤醒 — DONE**：接入正式 Conversation WebSocket，首帧认证，Worker 租约唯一连接、背压、断线退避和 Sandbox 回收；推送只唤醒，REST cursor 仍是唯一耐久事实。
- **T6.14 Bash WebSocket 与 REST 补偿 — DONE**：接入正式 Bash Event 唤醒并与直接 Bash/Agent terminal 事件来源区分；丢帧、重连、重复和多 Worker 竞争由 REST/正式状态补偿。

#### Tool Policy 与 Browser

- **T6.15 Tool Catalog 与策略约束 — DONE**：把目标镜像正式 Tool catalog、参数限制、读写分类、来源版本、确认要求和未知 Tool 拒绝编入不可变 Tool Policy；不开放动态 `tool_module_qualnames`。
- **T6.16 Tool 并发与正式工具集 — DONE**：治理并启用 grep/glob、planning editor、workflow、task 等产品所需 Tool；`tool_concurrency_limit` 默认 1，只有只读或具备资源锁的组合才允许并发，并覆盖 Workspace 竞争测试。
- **T6.17 Browser 网络与身份安全 — SKIP**：以节点 Policy 冻结 Browser 开关、egress allowlist、DNS/IP/重定向复核、SSRF/内网/跨 Runtime 拒绝、Cookie/登录态隔离、资源和超时限制。
- **T6.18 Browser Artifact 与确认闭环 — SKIP**：截图、录屏、下载进入受分类 Artifact；导航/上传/下载/写操作使用明确 Confirmation policy，审计原生 Action identity，清理随 Runtime 生命周期恢复。
- **T6.19 Browser Runtime 接入收口 — SKIP**：完成固定目标镜像中的 Browser 依赖、受管网络配置、资源回收接口和 T9 测试夹具；导航、交互、截图、下载、确认拒绝、SSRF 拒绝和资源回收的真实功能链统一在 T9 执行。

退出条件：T6.01–T6.19 全部为 `DONE` 或 `SKIP`，覆盖账本同步，基本代码门禁通过后将 T6 标为 `IMPLEMENTED` 并把 T7.01 提升为唯一 `CURRENT`；所有功能、集成、恢复与真实 Runtime 验收继续留到 T9。

### T7 高级产品能力 — IMPLEMENTED

T7 按以下顺序实现配置、运行、事件/结果、恢复、审计、成本与安全代码；普通切片只跑基本代码门禁，完整闭环的行为验证统一在 T9。

#### Agent/LLM Profile

- **T7.01 Profile 治理与完整字段映射 — DONE**：在 T3 不可变模板基础上补齐 OpenHands Profile/LLM 字段兼容矩阵、发布/退役/复制/升级和 Secret 拒绝；Server 可变 Profile Store 仍不得成为生产真相。
- **T7.02 Profile 激活与切换语义 — DONE**：把“激活/切换”实现为显式选择不可变 Profile Version 并生成新 Snapshot/Attempt；运行 provenance、差异预览、回滚和模型成本对比可审计，不热改既有 Snapshot。

#### ACP Agent

- **T7.03 ACP 固定源码契约 — SKIP**：从固定 1.42.0 源码和镜像冻结 Agent kind、command/args、session、事件、MCP、模型切换、取消与恢复正式契约；缺口逐项 fail closed。
- **T7.04 ACP Runtime Provider 与 Agent Kind — SKIP**：新增独立 ACP Provider/Agent Kind、不可变配置和 Snapshot 编译，不复用原生 Agent 的 model 开关。
- **T7.05 ACP 命令白名单与凭据文件 — SKIP**：command/args 只能来自 Environment Version 白名单；认证文件经 Secret Reference 临时只读物化，禁止用户命令、环境变量和任意路径注入。
- **T7.06 ACP Session 生命周期 — SKIP**：映射 ACP session、FlowWeave Conversation、Workspace 和 Sandbox，闭环 create/resume/timeout/process-exit/cancel/重启恢复。
- **T7.07 ACP Tool/Event/Usage 归一化 — SKIP**：只使用正式 identity 投影 Tool、消息、错误、usage/cost 和模型切换；未知事件 fail closed，不按文本猜测。
- **T7.08 ACP Capability 兼容矩阵 — SKIP**：逐项验证 Skill、MCP、Hook、Artifact 与 Confirmation 的兼容性；不兼容绑定在发布和启动双重拒绝。

#### 直接 Runtime API 与 IDE/Desktop

- **T7.09 直接 Bash 操作者通道 — SKIP**：接入正式 Bash API，命令必须来自有权限的人工操作并与 Agent terminal 事件明确分源；持久化操作者、命令摘要、结果摘要、幂等、取消和审计，禁止伪装成 Agent Action。
- **T7.10 File 上传/下载/搜索 — SKIP**：所有路径由 Attempt/Workspace allocation 服务端推导；文件类型、大小、恶意内容、符号链接、目录穿越和租户边界 fail closed，上传/下载进入 Artifact 分类。
- **T7.11 Git 只读诊断 — SKIP**：接入 changes/diff/commits/commit changes，仓库根由服务端推导，限制输出大小并脱敏；不得接受浏览器绝对路径或隐式写操作。
- **T7.12 Workspace 归档与受限静态读取 — SKIP**：归档绑定 Attempt/Snapshot/digest 和导出审批；静态读取只暴露 allowlist 文件/Artifact，不代理整个 Conversation 工作目录。
- **T7.13 Trajectory 导出 — SKIP**：接入正式 trajectory export，并叠加 Artifact 数据分类、Secret 扫描、审批、一次性下载和审计；不得只依赖上游已知字段脱敏。
- **T7.14 VSCode 安全访问 — SKIP**：一次性短期凭据、反向代理、SameSite/CSP、owner 绑定、端口隐藏、撤销和 Runtime 回收闭环；不直接返回上游裸 URL。
- **T7.15 Desktop 安全访问 — SKIP**：独立于 VSCode 验证授权、会话隔离、剪贴板/上传下载策略、并发和资源限制、撤销及回收。

#### Hook、Critic 与 Goal

- **T7.16 Hook Set 统一版本与验证 — DONE**：消除审计矩阵中的 Hook 缺口；Hook 作为不可变 Version 进入 Manifest，冻结兼容性、脚本摘要、事件类型、权限和安全验证，禁止运行时全局修改。
- **T7.17 Critic 自修复闭环 — DONE**：冻结评分对象、阈值、最大精炼次数和预算；投影正式 Critic 事件/得分/费用，崩溃恢复幂等，END Gate 继续作为独立业务判断。
- **T7.18 Goal loop 治理 — DONE**：接入正式 start/stop/resume，限制总轮次、Token、金额、并发和后台重试乘积；Goal 状态、终止原因和人工操作可审计。
- **T7.19 ask_agent 只读诊断 — DONE**：接入不修改 Conversation state 的正式查询，冻结权限、费用、超时、输出分类和审计，不作为绕过 Goal/Gate/cursor 的执行入口。

#### 第 11.17 节逐项处置

- **T7.20 Conversation search/count/title/tags — DECIDED_NO**：FlowWeave Conversation 目录、标题、标签和权限是唯一产品真相；不建立双向同步。固定契约探针可以读取这些接口做诊断，但生产 API/UI 不暴露、不反向覆盖平台状态。
- **T7.21 pause 与独立 run — DECIDED_NO**：当前 steer、cancel、Confirmation 和 Goal 已有各自状态语义；本次不新增一个与它们重叠的人工 pause/run 产品入口。适配器仅保留内部正式续跑调用及状态前置校验。
- **T7.22 agent_final_response — DECIDED_NO**：正式 Finish 事件、REST cursor 和输出契约继续是终态唯一事实；不得增加绕过去重与恢复模型的第二结果来源。
- **T7.23 Event count/get/batch — DECIDED_NO**：正常投影继续只走正式 REST cursor；契约探针可用于诊断，但本次不建立第二套事件修复 API。
- **T7.24 Worktree — DECIDED_NO**：上游 `/tmp` Worktree 不在 FlowWeave Workspace allocation、Snapshot 和清理台账中；保持不可达，回归测试禁止生产调用。若未来纳入，必须先建立受管资源账本后新建原子任务。
- **T7.25 Client Tools — DECIDED_NO**：用户确认的 Tool Policy 范围是受管 Runtime 内正式 Tool，不包含在浏览器客户端执行 Tool；双向 Action 回执会引入新的可信执行面，生产请求持续拒绝 Client Tools。
- **T7.26 tool_module_qualnames — DECIDED_NO**：禁止调用方动态导入服务端模块；只允许固定镜像 Tool allowlist，并用架构/请求测试防回归。
- **T7.27 Secrets API — DECIDED_NO**：生产通用 Secret 继续由 FlowWeave Secret Reference 唯一治理，禁止向 OpenHands 可变 Store 双写或传明文字典。
- **T7.28 LLM discovery/OpenAI subscription — DECIDED_NO**：Model Provider/Codex OAuth 继续由 FlowWeave 单一治理；只读兼容探针可存在，不同步可变 Settings。
- **T7.29 `/v1/chat/completions` — DECIDED_NO**：该入口缺完整 Conversation/Tool 生命周期，生产适配器持续禁止使用。
- **T7.30 Hooks API — DECIDED_NO**：运行时全局 Hook 修改会破坏 Snapshot；只允许 T7.16 冻结 Hook Set 随创建请求注入。
- **T7.31 Warm pool `/api/init` — DECIDED_NO**：当前没有冷启动容量指标证明产品需要共享预热池；本次保持每个受管 Runtime 完整隔离启动，禁止复用含上一租户状态的实例。达到明确规模阈值后另立产品任务。
- **T7.32 Workspace session Cookie — DECIDED_NO**：Worker 继续使用 Header API Key；VSCode/Desktop 使用 T7.14/T7.15 独立一次性凭据，禁止暴露 OpenHands UI Cookie。
- **T7.33 Server health/details 与能力协商 — DONE**：启动时校验版本、source provenance、正式路由/字段/capability，与 Snapshot 要求不兼容时拒绝调度，替代仅依赖 Compose healthcheck。

退出条件：除有明确回归约束的 `DECIDED_NO` 和正式证据支撑的 `UPSTREAM_BLOCKED` 外，T7.01–T7.33 全部为 `DONE`；随后将 T7 标为 `IMPLEMENTED`，把 T8.01 提升为唯一 `CURRENT`。

### T8 产品 API、UI 与文档收口 — IN_PROGRESS

- **T8.01 Marketplace 目录浏览与导入 UI — CURRENT**：浏览固定目录 commit、选择条目、展示双层 provenance/验证结果并发布 Version，不展示或加载浮动安装态。
- **T8.02 Tool Policy 与 Browser UI — CURRENT**：编辑 Tool allowlist、读写/确认/并发/网络/Artifact 策略，展示实际有效 Snapshot 与拒绝原因。
- **T8.03 Profile 与 ACP UI — CURRENT**：Profile Version 差异/激活、ACP Provider/兼容矩阵/Session 状态和成本可见，不暴露 Secret 或任意 command。
- **T8.04 原生分支与 Navigate UI — CURRENT**：明确 Native Fork、Semantic Fork 和 Navigate 风险，展示 source event/head、继承范围和审计。
- **T8.05 Usage、预算与 Trace UI — CURRENT**：Run/Node/Attempt/Conversation/Task 对账视图、费用拆分、预算状态和 trace 跳转，避免父子双计。
- **T8.06 实时连接与恢复 UI — CURRENT**：展示 WebSocket 仅为唤醒、REST cursor 同步状态、断线退化和恢复，不把连接状态当执行事实。
- **T8.07 Runtime 诊断与 Artifact UI — CURRENT**：Browser、Bash、File/Git/Workspace/Trajectory 的授权操作、来源标识、导出审批和生命周期状态。
- **T8.08 Critic/Goal/IDE/Desktop UI — CURRENT**：展示自修复/Goal 预算与事件，以及 VSCode/Desktop 一次性访问、撤销和回收状态。
- **T8.09 契约与文档一致性 — CURRENT**：逐项生成/比对 OpenAPI 和前端类型，删除旧 UI/文本分叉/固定 Tool/旧协议入口，更新系统设计、运维和安全文档及第 11 章覆盖账本。

退出条件：T8.01–T8.09 全部为 `DONE`，受影响文件的基本 lint/typecheck 和 API schema 可生成后标为 `IMPLEMENTED`；完整 Web build、API 行为和 E2E 留到 T9。

### T9 全量验证与清理 — PENDING

范围：集中执行 T1-T8 延后的全部行为单元、集成、契约、安全、恢复、真实 Runtime、容器 smoke 和 E2E；固定目标源码提交 `f09e03e...` 构建镜像的真实 smoke 及其相对历史 v1.40.0 的兼容差异；死代码、兼容层、旧迁移和旧测试清理；测试数据库与空库安装；按两份设计文档和路线图完成定义逐项验收。

最终验证门禁（不得拆散到普通切片重复运行）：

1. 平台：全仓 Ruff format/check、生产 Pyright、平台全量 pytest。
2. Web：lint、typecheck、生产 build。
3. 契约：生成 OpenAPI 并与基线一致，架构边界测试通过。
4. 数据库：空库、既有基线、历史数据升级、downgrade/upgrade 和当前 Alembic head 全矩阵通过。
5. OpenHands：镜像 provenance 精确匹配固定目标源码 commit/source digest，四个源码构建包和目标源码契约正确；`make openhands-contract-check` 与 `make openhands-smoke` 通过，并对历史 v1.40.0 兼容差异有显式基线。
6. 安全与恢复：Confirmation、Condenser、Task、Plugin/MCP、cursor、取消、预算、Secret 泄漏和 fail-closed 关键矩阵通过。
7. 产品：关键 API/UI E2E、文档与实际行为一致，`git diff --check` 通过，无未解释的死代码或旧协议入口。
8. 按 T1-T8 实现门禁逐项复核；通过者由 `IMPLEMENTED` 晋级 `COMPLETE`。外部阻塞项必须有最新可复现证据、已验证安全降级和明确解锁条件。

退出条件：上述最终门禁一次性全部通过，或外部阻塞满足第 8 条的显式例外；所有任务状态和两份持久化文档与最终证据一致。

## 新发现问题

- T2：OpenHands Conversation 默认 `NeverConfirm`；已通过冻结的节点/Attempt 策略和创建请求中的结构化 `confirmation_policy` 修复。
- T2：OpenHands 1.40.0 的确认单位是整个 pending action batch，不支持逐 Action 决策；UI 和 API 已限制为整批决定。
- T2：拒绝仅追加 `UserRejectObservation`，不会自动继续 Agent；已使用正式 `/run` 续跑，并限制仅从 `idle/paused` 启动以防崩溃重试重复执行。
- T2：历史基线迁移使用当前 ORM metadata 创建表；0025 必须同时兼容空库已带新列和旧库缺列两条路径，现已用 introspection guard 覆盖。
- T2：OpenHands `/condense` 在 1.40.0 中同步追加 `CondensationRequest` 并执行一次 Agent step；客户端超时仍可能形成“Runtime 已生效、平台未提交”窗口，现以先读原生事件、仅无 Request 时 POST 的重放策略闭环。
- T2：OpenHands event search 在 page anchor 不存在时从日志起点回退；适配器必须保留旧 cursor 并重试，禁止把旧历史投影成新一轮结果。
- T3：`CapabilityImport` 不能继续充当永久版本；发布、编辑和依赖构建现均产生不可变 Version，Import 只用于来源审计。
- T3：早期 baseline 迁移使用当前 ORM metadata，空库往返无法证明旧库安全；迁移检查现显式还原 0028 列形态并插入旧复合引用与 Snapshot 数据。
- T3：依赖构建结果不能原地更新已发布配置或按 capability key 批量重绑节点；现改为派生 READY Version，由调用方显式选择升级。
- T3/T5：OpenHands 1.40.0 的 `agent_definitions` 只注册定义，主 Agent 还必须显式启用高层 `task_tool_set`；低层 `task` 需要内部 Executor，继续 fail closed。当前已完成定义治理和原生请求，原生 Task 生命周期投影仍待 T5。
- T3/T4：OpenHands 会从用户目录和项目目录自动发现 ambient Plugin，且运行中 `load_plugin` 会改变 Tool/上下文；固定镜像现禁用 ambient 发现，生产 RuntimePort 不暴露动态加载，只允许 Snapshot 显式传入的只读本地 PluginSource。
- T5：OpenHands 1.40.0 的 Task Tool 在父进程内阻塞执行 `LocalConversation`，公开 `/api/sub-agents` 仅发现 Agent Definition；正式父事件只有请求 `TaskAction` 和终态 `TaskObservation`，没有运行中 Task 列表、单 Task 取消、异步确认或独立成本 API。本切片只投影 1.40.0 可证明的生命周期，不虚构 child conversation/control contract。
- T5：`TaskToolSet.create` 虽接受可选 `confirmation_handler`，但固定 1.40.0 镜像已执行证明 Agent Server 使用的公共 Tool registry 对正式空参数 `Tool` spec 只传 `conv_state`，生成的 `TaskManager._confirmation_handler` 为 `None`；此时 Manager 会自动继续处于 `waiting_for_confirmation` 的子 Agent。Agent Definition 现强制 `never_confirm`；目标源码镜像完成后重新审计其正式契约，已有能力只改 FlowWeave 接入，仍缺失则继续 fail closed。
- T5：Task 成本不需要私有子会话 API；1.40.0 在结束前把独立子 metrics 以累计替换语义写入父 Conversation 的 `task:<task_id>` usage key。该键可与正式 `TaskObservation.task_id` 关联，是下一成本投影切片的唯一事实来源。
- T5：Task resume 复用原生 `task_id` 并产生新的 Action/Observation invocation；成本账本必须以 `(conversation_id, task_id)` 唯一并指向最新 invocation，不能把同一累计快照复制到每次调用。
- T5：父 Conversation stats 与事件页没有原子读取端点；现在 AUTO Attempt 与人工会话轮询都会在任一侧先到时保留正式 `task_id` 待另一侧可见，最多有界重读 5 次，成功/失败父终态在账本闭环前不会提交；耗尽后记录审计并 fail closed，已知累计值回退仍拒绝。
- T5：父 `/interrupt` 取消 `arun()` 后只对工具调用 `executor.interrupt()`；1.40.0 的 `TaskExecutor` 未覆盖默认 no-op，已启动同步 Task 会继续在线程中执行。共享 Runtime 因而只能报告取消未确认；受管独立 Runtime 必须等物理资源删除后才能证明整 Runtime 停止。
- T5：Task `resume` 使用当前 `TaskManager._tasks` 的进程内身份表；新 Manager 不会从持久目录重建 Task 表，因此服务重启后的单 Task 恢复尚无正式契约。
- T3：OpenHands 1.40.0 的 `agent_profile_id` 会从 Agent Server 的可变 Profile、LLM 和 MCP Store 解析，且与显式 `agent` / `agent_settings` 互斥。生产运行必须把已冻结 Profile 完全物化为显式 Agent JSON；Profile UUID/version/digest 只作为 FlowWeave provenance 和观测关联，不能成为 Runtime 的第二配置真相。
- T6：固定目标 1.42.0 的 `AgentContext.load_memory` 只有一个布尔开关；开启时同时读取用户目录和工作区的 `.openhands/memory/MEMORY.md`，不存在正式的 tier allowlist、期望 digest 或必读成功契约。FlowWeave 因此只在冻结治理内容已按运行 scope 隔离物化并挂载到受管 Docker Runtime 时启用；其他路径继续 fail closed。
- T6：Memory Source 保留期必须在 Version 激活时冻结，不能在退役或删除时回读可变 Policy。ACTIVE 不到期；退役时才计算 `expires_at`。不可变 Policy/Snapshot 引用必须以数据库可验证的 `MemorySourceVersion.id + digest` 为事实，任意 UUID 或仅扫描 JSON 的临时判断不足以形成删除保护。

## 验证日志

| 时间 | 范围 | 命令 | 结果 |
|---|---|---|---|
| 2026-08-12 | 初始化 | `git status --short --branch` | PASS：开始前工作树干净 |
| 2026-08-12 | Python 格式与 Lint | `ruff check ... && ruff format --check ...` | PASS |
| 2026-08-12 | 目标类型检查 | `pyright --pythonpath services/platform/.venv/bin/python ...` | PASS：0 errors |
| 2026-08-12 | OpenHands 1.40.0 适配器契约 | `pytest -q services/platform/tests/test_openhands.py` | PASS：36 passed |
| 2026-08-12 | Web | `pnpm typecheck && pnpm build` | PASS；仅保留既有大 chunk 警告 |
| 2026-08-12 | PostgreSQL 空库迁移与 OpenAPI | `pytest -q tests/contract/test_contracts.py -k generated_openapi` | PASS：真实 Testcontainers PostgreSQL 升级到 0025，1 passed |
| 2026-08-12 | API/Worker/Conversation 目标回归 | `pytest -q tests/test_api.py tests/test_tasks.py tests/test_conversations.py -k ...` | PASS：55 passed |
| 2026-08-12 | OpenHands 镜像契约 | `make openhands-contract-check` | PASS：四包 1.40.0、14 条正式路由、27 个创建字段及关键默认值/类型 |
| 2026-08-12 | Phase 0 目标回归 | `pytest -q tests/test_openhands.py tests/test_conversations.py tests/test_tasks.py tests/architecture/test_boundaries.py` | PASS：90 passed |
| 2026-08-12 | PostgreSQL 迁移往返 | `uv run python scripts/migration_check.py` | PASS：空库升级至 0028、降级并再次升级 |
| 2026-08-12 | Cursor/Condense 恢复 | 定向 OpenHands/Conversation 测试 | PASS：anchor 竞态、无 WebSocket REST 投影及三种 Condense 重放窗口 |
| 2026-08-12 | 真实 OpenHands 1.40.0 | `make openhands-smoke` | PASS：原生 Confirmation 与 Condenser 两条真实协议链；无外部模型依赖且自动清理 |
| 2026-08-12 | T3 不可变能力仓库 | Catalog、Collection、Worker 与 Runtime Manifest 定向测试 | PASS：Version UUID、Skill 新版本、依赖派生版本、Snapshot 冻结和篡改拒绝 |
| 2026-08-12 | PostgreSQL 历史迁移 | `uv run python scripts/migration_check.py` | PASS：0024–0030 空库往返；0028 旧 Import/节点/Snapshot 数据升级并回填一致 Version/digest/Blob |
| 2026-08-12 | T3 全量验证 | `pytest -q`、Ruff、Pyright、`pnpm typecheck`、`pnpm build` | PASS：平台 316 项测试；前端构建仅保留既有大 chunk 警告 |
| 2026-08-12 | T3 Tool Policy / RuntimeAgentSpec | 定向 98 项 pytest、Ruff、生产 Pyright、`uv run python scripts/migration_check.py` | PASS：98 passed、0 类型错误；0031 在真实 PostgreSQL 完成空库升级、全链往返和 0028 历史基线重放 |
| 2026-08-12 | T3 Plugin / 原生 Loader | Plugin 导入/物化/Adapter/Snapshot 定向测试、相关 119 项 pytest、Ruff、生产 Pyright、Web typecheck/build、OpenAPI、迁移检查 | PASS：固定 Version/digest/file manifest、只读挂载、对象漂移拒绝、Snapshot 恢复和原生 `plugins` 请求通过 |
| 2026-08-12 | T3 Plugin / OpenHands 1.40.0 | `make openhands-contract-check && make openhands-smoke` | PASS：四包固定 1.40.0；ambient Plugin 禁用补丁进入镜像；真实 Confirmation/Condenser smoke `status=ok` |
| 2026-08-12 | T5 原生 Task 生命周期投影 | `pytest -q tests/test_openhands.py tests/test_conversations.py tests/test_tasks.py tests/test_capability_imports.py tests/architecture/test_boundaries.py` | PASS：133 passed；正式事件关联、乱序/重放/漂移拒绝、旧 delegation JSON 不执行 |
| 2026-08-12 | T5 平台全量与静态验证 | `ruff format --check . && ruff check . && pytest -q`、生产 `pyright` | PASS：362 passed；0 类型错误 |
| 2026-08-12 | T5 Web 与 API | `pnpm ... lint && typecheck && build`、`pytest -q tests/contract/test_contracts.py -k generated_openapi` | PASS：Web 构建仅有既有大 chunk 警告；OpenAPI 1 passed |
| 2026-08-12 | T5 PostgreSQL 历史迁移 | `uv run python scripts/migration_check.py` | PASS：空库、0005 往返和 0028 历史数据路径均升级到 0036；确认 Task 表无 prompt、旧委派协议列已删除 |
| 2026-08-12 | T5 固定 OpenHands 1.40.0 | `make openhands-contract-check && make openhands-smoke` | PASS：四包固定 1.40.0；真实 Confirmation、Condenser、原生 Task Tool 三条链 `status=ok` |
| 2026-08-12 | T3 Agent Profile 协议闭环 | 定向 Profile/Adapter/架构测试、Ruff、生产 Pyright、平台全量 pytest、Web lint/typecheck/build、OpenAPI、迁移检查 | PASS：Profile version/digest 进入 RuntimePort 和 observability metadata；生产 Adapter 静态禁止 Server Store lookup；平台 365 passed、0 类型错误 |
| 2026-08-12 | T3 Agent Profile / OpenHands 1.40.0 | `make openhands-contract-check && make openhands-smoke` | PASS：正式 Profile schema v2、16 字段、五条路由、默认值与 `agent`/`agent_profile_id` 互斥均由镜像安装产物验证；三条真实 smoke 链 `status=ok` |
| 2026-08-12 | T5 原生 Task 治理契约基线 | `make openhands-contract-check` | PASS：四包固定 1.40.0；同步 Task executor、状态集合、无单 Task 控制路由、进程内 resume、确认回调/自动继续语义、累计 `task:<task_id>` metrics 替换均由镜像安装产物执行验证 |
| 2026-08-12 | T5 Agent Definition Confirmation fail closed | `uv run pytest -q tests/test_capability_imports.py tests/test_openhands.py tests/test_tasks.py tests/architecture/test_boundaries.py -k 'agent_definition or governed_agent_definitions or native_subagent or subagent or profile'` | PASS：12 passed；继承、`always_confirm`、`confirm_risky` 均拒绝，显式 `never_confirm` 的冻结与原生请求通过 |
| 2026-08-12 | T5 Task stats / 真实 OpenHands 1.40.0 | `make openhands-smoke` | PASS：Confirmation、Condenser、Task 三条链 `status=ok`；真实父 Conversation stats 包含与 `TaskObservation.task_id` 对应的 `task:<task_id>` 累计 token metrics；Condenser 完成事件使用有界 cursor 轮询消除可见性竞态 |
| 2026-08-12 | T5 本切片平台全量 | `uv run pytest -q`、`uv run ruff format --check . && uv run ruff check .`、生产 `pyright`、`git diff --check` | PASS：368 passed；211 files formatted；0 类型错误；无 whitespace 错误 |
| 2026-08-12 | T5 Task usage/cost 投影 | `uv run pytest -q tests/test_openhands.py tests/test_conversations.py tests/test_tasks.py tests/architecture/test_boundaries.py` | PASS：112 passed；正式 stats 规范化、累计替换、重放、回退拒绝、resume 单账本和预算越界审计通过 |
| 2026-08-12 | T5 Task usage/cost 全量与静态 | `uv run pytest -q`、`uv run ruff format --check . && uv run ruff check .`、生产 `pyright`、Web lint/typecheck/build、OpenAPI、`git diff --check` | PASS：374 passed；212 files formatted；0 类型错误；Web 仅既有大 chunk 警告；OpenAPI 1 passed |
| 2026-08-12 | T5 Task usage/cost PostgreSQL | `uv run python scripts/migration_check.py` | PASS：空库、0005 全链往返和 0028 历史路径均升级至 0037；usage 账本唯一键、非负约束且无 prompt 字段 |
| 2026-08-12 | T5 Task usage/cost OpenHands 1.40.0 | `make openhands-contract-check && make openhands-smoke` | PASS：固定四包 1.40.0；累计 `task:<task_id>` 替换契约与真实 Task stats 链 `status=ok` |
| 2026-08-12 | T5 Task stats/event 可见性恢复 | `uv run pytest -q tests/test_openhands.py tests/test_conversations.py tests/test_tasks.py tests/architecture/test_boundaries.py` | PASS：116 passed；成功/失败、事件先到、stats 先到、有界耗尽、重放和累计替换通过 |
| 2026-08-12 | T5 本切片全量与静态 | `uv run ruff format --check . && uv run ruff check . && PYRIGHT_PYTHON_FORCE_VERSION=1.1.405 uv run pyright && uv run pytest -q`、Web lint/typecheck/build、OpenAPI、`git diff --check` | PASS：378 passed；212 files formatted；0 类型错误；Web 仅既有大 chunk 警告；OpenAPI 1 passed |
| 2026-08-12 | T5 本切片 PostgreSQL / OpenHands 1.40.0 | `uv run python scripts/migration_check.py`、`make openhands-contract-check && make openhands-smoke` | PASS：空库、0005 全链往返和 0028 历史路径至 0037；固定四包 1.40.0，三条真实链 `status=ok` |
| 2026-08-12 | T5 父级取消安全边界 | `make openhands-contract-check`、取消/Task 定向 pytest | PASS：镜像内阻塞 Task 在父 interrupt 后仍存活；共享 Runtime fail closed、受管 Runtime 等待物理删除、无 Task 原行为共 20 项定向回归通过 |
| 2026-08-12 | T5 取消边界全量与静态 | `uv run pytest -q`、Ruff format/check、生产 Pyright、Web lint/typecheck/build、OpenAPI、`git diff --check` | PASS：380 passed；212 files formatted；0 类型错误；Web 仅既有大 chunk 警告；OpenAPI 1 passed |
| 2026-08-12 | T5 取消边界 PostgreSQL / OpenHands 1.40.0 | `uv run python scripts/migration_check.py`、`make openhands-smoke` | PASS：迁移全链仍至 0037；固定四包 1.40.0，三条真实链 `status=ok` |
| 2026-08-12 | T5 取消耐久恢复与产品闭环 | 取消恢复定向 pytest；平台全量 pytest；Ruff format/check；生产 Pyright；Web lint/typecheck/build；OpenAPI；迁移检查；`make openhands-contract-check && make openhands-smoke`；`git diff --check` | PASS：4 条核心恢复回归、17 条取消/Task 定向回归和平台全量 382 项通过；212 个 Python 文件格式/Lint 通过、0 类型错误；OpenAPI 1 项通过；空库、0005 往返和 0028 历史路径均至 0037；固定四包 1.40.0，真实 Confirmation/Condenser/Task 三链 `status=ok`；Web 仅既有大 chunk 警告 |
| 2026-08-12 | T5 Task 异步确认上游缺口负向契约 | `uv run ruff format --check ../../infra/openhands/contract_check.py && uv run ruff check ../../infra/openhands/contract_check.py`、`make openhands-contract-check` | PASS：固定四包 1.40.0；正式 `Tool` spec 经公共 registry 创建的 Task manager 无 confirmation handler，输出 `task_registry_confirmation_handler=false`；T5 继续 fail closed |
| 2026-08-12 | 验证门禁分层与项目 Skill | `uv run python .../skill-creator/scripts/quick_validate.py ../../.agents/skills/flowweave-refactor`、状态唯一性检查、`git diff --check` | PASS：项目级 Skill 有效；全局副本已移除；仅 T3 为 `IN_PROGRESS`；T1/T2 转为 `IMPLEMENTED`，全量验证集中到 T9 |
| 2026-08-12 | OpenHands 源码治理边界 | 项目 Skill 校验、任务状态唯一性检查、`git check-ignore -v AGENTS.md`、`git diff --check` | PASS：根 `AGENTS.md` 已进入项目工作树；后续允许单独授权二开，但当前主链只修改 FlowWeave；仅 T3 为 `IN_PROGRESS` |
| 2026-08-12 | T3 OpenHands source-built bootstrap | `uv run pytest -q tests/test_openhands_source_supply.py tests/architecture/test_boundaries.py -k 'source_lock or source_fetch or source_archive or openhands_runtime_uses_digest_locked_source_build or openhands_image_runs_installed_contract_probe'`、改动文件 Ruff、`make openhands-contract-check` | PASS：5 项定向测试；固定历史 upstream commit `2f276539...04e`、归档 SHA-256 `1eac9d...fa70a`；四包从 `/opt/openhands-source` 构建，镜像内 provenance/`direct_url.json`/1.40.0 契约通过；image ID `sha256:579746...bebad`。下一切片切换到固定目标源码提交 |
| 2026-08-12 | 当前源码适配边界 | Skill 校验、固定源码 HEAD 取证、任务状态唯一性检查、`git diff --check` | PASS：当前适配目标冻结为 `f09e03eac772290feeb51b7d7390ffaefeca1a09`；T1-T9 只修改 FlowWeave，OpenHands 工作树只读，二开移出当前主链 |
| 2026-08-12 | T3 固定目标源码供应链 | `uv lock`；供应链/架构 5 项定向 pytest；改动文件 Ruff format/check；`make openhands-contract-check` | PASS：归档 SHA-256 `a33dfae9...c832`；四包均从 `/opt/openhands-source` 构建为 `1.42.0`；provenance、`direct_url.json` 和现有最小正式契约探针通过；镜像 ID `sha256:a37f3d7a...a35f9`。完整 smoke、历史兼容矩阵和平台全量回归延后到 T9 |
| 2026-08-12 | T4 MCP OAuth Secret Reference | `uv run pytest -q tests/test_mcp_validations.py`、`uv run pytest -q tests/test_openhands.py -k openhands_probes_mcp_through_target_runtime_and_redacts_oauth_state`、`uv run pytest -q tests/contract/test_contracts.py -k generated_openapi` | PASS：OAuth/MCP 8 项、Adapter 1 项、OpenAPI 1 项；覆盖加密保存、正式 state 往返、刷新 CAS、撤销、审计脱敏、内嵌 Secret/损坏密文/摘要漂移拒绝 |
| 2026-08-12 | T4 MCP OAuth 迁移与固定契约 | 临时 PostgreSQL `0038 -> 0039` 升级、`alembic heads`、改动文件 Ruff format/check、`make openhands-contract-check` | PASS：真实 PostgreSQL 完成 0038 到 0039 升级且 0039 为唯一 head；固定四包 1.42.0 证明 OAuth state 输入位于 `server.auth.state`、输出位于成功响应顶层 `oauth_state`；镜像 ID `sha256:8a2d26b4...f481b` |
| 2026-08-13 | T4 MCP OAuth 首次浏览器授权 | 改动文件 Ruff format/check、窄范围 Pyright、OAuth/Adapter/Sandbox/OpenAPI 定向 pytest、`git diff --check` | PASS：11 项定向测试、0 类型错误；耐久 job、双版本 CAS、callback 不落库、token/client secret 脱敏、撤销 fencing、Sandbox owner 分派与 post-commit 回收通过 |
| 2026-08-13 | T4 MCP OAuth 0040 与固定契约 | 临时 PostgreSQL `0039 -> 0040` 升级、`alembic heads`、OpenAPI 基线、`make openhands-contract-check` | PASS：0040 为唯一 head；固定四包 1.42.0 的正式 `start/status/callback` 路由与模型字段通过；source-built 镜像 ID `sha256:565de04e...31c98` |
| 2026-08-13 | T4 Plugin 目标环境原生 Loader | 改动文件 Ruff format/check；Plugin/Catalog/Controller/Adapter 与架构定向 pytest；窄范围 Pyright；OpenAPI 生成/比对；固定镜像最小 `Plugin.load` smoke；`make openhands-contract-check` | PASS：目标 Runtime 成功解析 manifest 和 1 个 Command；路由/共享层双重 validation 路径约束、Sandbox ownership、任意命令字段拒绝、失败耐久化和元数据脱敏通过；定向 26 项及最终聚焦 7 项通过，0 类型错误；四包 1.42.0 source-built 契约通过，镜像 ID `sha256:af7a612e...5015c` |
| 2026-08-13 | T4 固定 Marketplace Plugin 来源治理 | 改动文件 Ruff format/check；Plugin 来源、resolver、Controller 与架构定向 pytest；窄范围 Pyright；OpenAPI 生成/比对；临时 PostgreSQL `0040 -> 0041`；`alembic heads`；`make openhands-contract-check`；`git diff --check` | PASS：最终 37 项定向测试、0 类型错误；固定目录 commit 与实际 Plugin commit 双层 provenance、allowlist/路径/漂移拒绝、worker-only Controller 路由、旧 Git 行回填和 Runtime 远程来源隔离通过；0041 为唯一 head；四包 1.42.0 正式 Marketplace 类型与 `resolved_ref` 探针通过，镜像 ID `sha256:84a544...44f1` |
| 2026-08-13 | T4 Skill 原生触发/invoke | 改动文件 Ruff format/check；窄范围 Pyright；Skill Adapter/事件/消息/导入 5 项定向 pytest；`make openhands-contract-check`；`git diff --check` | PASS：冻结 AgentSkills、结构化选择到原生 `KeywordTrigger`、已有触发词去重、`activated_skills` 与 `InvokeSkillAction` / `InvokeSkillObservation` 投影、正文/目录旁路删除通过；固定四包 1.42.0 实际执行 activation/invoke 与 `invoked_skills` 去重，镜像 ID `sha256:1dc0a4...e89e2`；0 类型错误、无 whitespace 错误 |
| 2026-08-13 | T6 Memory 正式契约边界 | `uv run ruff format infra/openhands/contract_check.py`、`uv run ruff check infra/openhands/contract_check.py`、`make openhands-contract-check` | PASS：固定四包 1.42.0 实际合并 User/Project 两层索引，并证明损坏 UTF-8 索引只告警后继续；输出 `memory_tiers_independently_selectable=false`、`memory_read_failure_is_fatal=false`，镜像 ID `sha256:e99c64...515b`；启用态继续 fail closed |
| 2026-08-13 | T6 Memory Source 不可变内容模型 | `uv run pytest -q tests/test_memory_sources.py`、改动文件 Ruff format/check、`uv run pytest -q tests/contract/test_contracts.py -k generated_openapi`、临时 PostgreSQL `0041 -> 0042`、`alembic heads`、`git diff --check` | PASS：4 项定向测试与 OpenAPI 1 项通过；规范 UTF-8/digest、追加式版本、治理初态、正文脱敏、创建方伪造治理状态拒绝，以及数据库身份/内容/版本链防篡改通过；0042 为唯一 head；Runtime 仍 fail closed |
| 2026-08-13 | T6 Memory Source 审查、扫描与激活 | `uv run pytest -q tests/test_memory_sources.py`、改动文件 Ruff format/check、生产文件窄范围 Pyright、`uv run pytest -q tests/contract/test_contracts.py -k generated_openapi`、带既有行的临时 PostgreSQL `0042 -> 0043`、`alembic heads`、`git diff --check` | PASS：8 项定向测试与 OpenAPI 1 项通过、0 类型错误；治理 CAS、owner 自审/缺失主体拒绝、服务端扫描、敏感值脱敏、双门禁、唯一 ACTIVE、旧版本原子退役和数据库绕过拒绝通过；0043 为唯一 head；Runtime 仍 fail closed |
| 2026-08-13 | T6 Memory Source 过期与删除治理 | `uv run pytest -q tests/test_memory_sources.py`、改动文件 Ruff format/check、生产文件窄范围 Pyright、`uv run pytest -q tests/contract/test_contracts.py -k generated_openapi`、带既有 ACTIVE/RETIRED 行的临时 PostgreSQL `0043 -> 0044 -> 0043 -> 0044`、`alembic heads`、`git diff --check` | PASS：13 项定向测试与 OpenAPI 1 项通过、0 类型错误；冻结保留期、提前过期拒绝、EXPIRED/DELETED 墓碑、正文不可恢复、真实 Policy/Snapshot `version_id + digest` 引用保护及数据库绕过拒绝通过；无擦除时迁移往返通过，发生不可恢复擦除后 downgrade 明确拒绝；0044 为唯一 head；Runtime 仍 fail closed |
| 2026-08-13 | T6 Memory Source 隔离物化 | `uv run pytest -q tests/test_memory_sources.py tests/test_sandboxes.py`、任务启动与 Controller 定向 pytest、核心安全节点 7 项、改动文件 Ruff format/check、生产文件窄范围 Pyright、`git diff --check` | PASS：完整 Memory/Sandbox 70 项、任务启动 13 项、Controller 32 项和核心安全边界 7 项通过，0 类型错误；真实 Run Snapshot 自动登记 retention hold，固定 Source 在 Attempt/Conversation owner 下按 USER/PROJECT 原子物化并回读 digest/UTF-8，只向受管 Docker bind/named-volume 正式路径只读挂载；分配失败、立即删除和后台 reconcile 均按生命周期清理正文副本 |
| 2026-08-13 | T6.06 原生 Conversation Fork | 固定 1.42.0 镜像 Fork 请求/服务契约探针；改动文件 Ruff format/check；生产文件窄范围 Pyright；应用 OpenAPI schema 生成/基线比对；`uv run python scripts/migration_check.py`；`alembic heads`；`git diff --check` | PASS：正式 `/fork` 的 `id`、`reset_metrics`、`from_event_id` 和来源/HEAD identity 契约通过；平台预分配目标 UUID，冻结源 HEAD/Event 并以 409 后正式身份核对恢复，漂移 fail closed；共享 Runtime Sandbox 引用保护通过静态门禁；0 类型错误；OpenAPI 86 paths；空库、往返和历史路径升级至唯一 `0045_runtime_conversation_forks` head。行为、恢复竞态与真实 fork E2E 留到 T9 |
| 2026-08-13 | T6.15–T6.16 Tool Catalog / 并发治理 | 改动文件 Ruff format/check、Python compile、窄范围 Pyright、Tool Policy 最小拒绝 pytest、`uv run python scripts/migration_check.py`、`alembic heads`、`make openhands-contract-check`、`git diff --check` | PASS：未知/禁用 Tool、串行 Tool 并发和写 Tool 关闭确认均拒绝；只读 Tool 并发策略可冻结；0 类型错误；空库、0005 往返与 0028 历史路径均升级至唯一 `0046_tool_policy_catalog` head；固定四包 1.42.0、15 项 catalog、并发字段、资源锁与空动态模块映射契约通过，镜像 ID `sha256:9c3ddab2...d7dce`。Workspace 竞争和真实 Runtime 功能验收留到 T9 |
| 2026-08-13 | T7.01–T7.02 Profile 治理与不可变切换 | 改动文件 Ruff format/check、Python compile、窄范围 Pyright、OpenAPI 生成、可变 Store/Secret 最小拒绝探针、`alembic heads`、`git diff --check` | PASS：固定 Profile schema v2 的 16 字段形成兼容矩阵；可变 LLM/Profile Store、顶层及嵌套 Secret 均拒绝；Profile 支持追加式修订/复制/退役/历史/绑定查询；固定 Version/digest 的切换生成新 Snapshot 与 Attempt并保存差异、成本对比和回滚指针；0 类型错误，OpenAPI 94 paths，迁移仍为唯一 `0046_tool_policy_catalog` head。行为、恢复和真实 Runtime 验收留到 T9 |
| 2026-08-13 | T7.33 Server health/details 与能力协商 | 改动文件 Ruff format/check、Python compile、窄范围 Pyright、6 项最小拒绝 pytest、OpenAPI 生成/基线比对、`alembic heads`、`make openhands-contract-check`、状态唯一性检查、`git diff --check` | PASS：固定四包 1.42.0、source commit/ref、正式 HTTP operation、创建字段、Server capability 结构与 Snapshot 实际 Tool 集进入不可变 Runtime contract；每次 Conversation 创建前重新协商，缺失/漂移在零创建副作用时 fail closed；OpenAPI 97 paths，0047 为唯一 head；固定镜像 ID `sha256:059cbae5eeec46be007f5b477cc8c8af018e684ac719a16678adda1b800c3bef`。真实 Runtime 与完整恢复验收留到 T9 |
