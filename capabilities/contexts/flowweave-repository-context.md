# FlowWeave 仓库上下文

> **用途**：本 Context 为参与 FlowWeave 仓库的需求分析、设计、开发、测试与代码评审的 Agent 提供产品边界、协作约定与导航入口。
>
> **边界**：本 Context 是协作导航和工程约束摘要，不替代仓库中更靠近目标文件的 `AGENTS.md`、源码、测试、迁移、运行记录或平台在线接口契约。发生冲突时，以离目标文件最近的 `AGENTS.md`、当前实现和服务端返回为准。

## 项目定位

FlowWeave 是 Agent 执行的治理控制面，不是执行器本身。它负责能力治理、不可变版本、权限、策略、审批、审计、资源隔离和业务投影；OpenHands 是受平台调度的执行依赖。

核心资源关系：

```text
能力（Skill / MCP / Plugin / Context / Agent Definition / Hook）
  └─ 受治理版本、digest、blob/hash
节点资产（字段、提示词、目录） ──> 流程定义 Flow
终端环境 Environment ──发布──> 不可变 Environment Version
Flow + READY Environment Version ──> FlowRun
FlowRun ──> NodeRun / Attempt / Artifact / Gate / 节点会话

Agent Workspace 是独立的交互式 Agent 工作台路径，不等同于 FlowRun 节点会话。
```

## 开始工作前

1. 阅读仓库根目录 `AGENTS.md`，以及目标目录中更近的 `AGENTS.md`。
2. 查看当前工作树、未提交 diff 与相关测试；不要覆盖其他协作者的改动。
3. 阅读相关源码、类型、接口、迁移和测试后再实施修改。
4. 优先做单一、最小且可独立验证的改动；避免顺手重构无关模块。
5. 修改后运行与变更范围相称的最窄验证，并说明未运行的验证及原因。

## 产品与架构边界

- 不通过 Docker、数据库、Runtime Provider 或 OpenHands 私有接口绕过 FlowWeave 平台。
- 能力必须经平台导入并形成可追溯版本；不得仅复制文件到 Runtime、OpenHands HOME 或项目目录而宣称已发布。
- Flow 是可编辑模板，不绑定环境版本；创建 FlowRun 时必须显式选择 READY 的 Environment Version。
- Environment Version 是不可变快照。变更环境须建立并发布新版本，不改写既有运行的冻结版本。
- Runtime 能力必须能追溯到固定版本、digest、blob/hash 与冻结 Manifest；明文 Secret 不得进入 Runtime 持久化内容。
- 事件关联只使用 OpenHands 的正式标识和 cursor，不依据名称、显示顺序、时间或文本推测资源身份。
- Agent Workspace 与 FlowRun 节点会话的生命周期、路由和数据投影必须保持边界清晰。

## 交互与 UI 设计原则

- 以当前主体内容为视觉重心，保持页面整体视觉平衡。
- 不使用大标题和冗余说明；可在一行表达的信息不拆成多个区域占用主体空间。
- 所有弹窗、浮层和菜单均支持点击外部区域与按 `Escape` 关闭。
- 每个 Tab 必须保有独立页面快照：离开后返回时恢复原有页面状态，不重置。
- 使用 `Command` 或 `Option` 的快捷键，必须提供 Windows 对应的 `Ctrl` 或 `Alt` 支持。
- 产品页面不得直接呈现浏览器默认样式的文本域、下拉框或选择框。原生 `<textarea>` 可以作为内部编辑实现，尤其适用于会话输入、IME、多行编辑和无障碍场景；它必须由 FlowWeave 自定义组件或样式封装，提供一致的视觉、焦点、尺寸、快捷键和可达性交互，且不得为形式替换而破坏已验证的编辑体验。筛选、配置和表单选择器仍禁止原生 `<select>`、`<option>` 与浏览器默认下拉界面。
- 设计功能或页面前，先参考相近的现有实现；优先继承已验证的布局、状态、键盘可达性和边界处理方式。
- 选择器必须具备键盘操作、`Escape` 关闭、点击外部关闭，以及滚动容器和视口边界的正确定位行为。

## 会话与状态设计要点

- 会话状态、未读状态、运行状态和 hydration 数据均以服务端正式投影与 OpenHands 正式事件为事实来源；浏览器本地存储只保存展示偏好等非事实数据。
- 异步回调、流订阅和延迟响应必须携带并校验会话 binding 身份，防止旧会话结果污染新会话。
- 初次打开或失效后的会话应显示明确加载状态，完成同批正式 hydration 后一次性渲染；不得用残缺历史或缓存推断“正在思考”。
- 正式最终消息只从 OpenHands `MESSAGE` 事件渲染；浏览器不能以流式文本 delta 伪造最终回复。
- 弹层的捕获阶段 `Escape` 关闭优先于工作台级快捷键；任何全局快捷键必须尊重已 `preventDefault` 的事件。
- Tab、草稿、侧栏选择和详情页应在合法范围内保留用户离开时的上下文，避免因切换或刷新而意外丢失工作状态。

## 数据与安全

- 不提交、打印、记录或在请求示例中包含密码、会话 Cookie、API Key、OAuth token、恢复码或其他明文 Secret。
- 用户隔离的控制面数据必须同时维护应用授权边界、数据库 RLS 和相关的隔离表登记。
- 高风险写操作必须限制目标范围、具备必要确认、保留审计信息并能报告失败结果。
- 删除、取消、完成、拒绝、撤销、覆盖更新和 Runtime 替换都会改变平台状态；先读取精确目标与引用关系，再进行最小操作。
- 生产部署、远端配置和基础设施细节不得进入仓库、能力 Context、日志或普通协作文档。

## FlowRun 与运行时

- FlowRun、NodeRun、Attempt、Artifact、Gate 和节点会话的身份以运行详情中的正式记录为准。
- 逐步节点启动应由平台原子预留节点会话绑定；前端使用返回的 FlowRun、NodeRun、Attempt 和 binding 身份进入工作台，不等待或猜测原生会话 ID。
- 运行中的会话视觉状态应以正式事件单调推进，不能因短暂的 Runtime readiness 波动回退。
- OpenHands 原生继续仅适用于正式允许恢复的状态；不得将已完成会话当成可重启会话。
- 可恢复网络类错误的重连必须遵循正式错误分类、尝试上限和新进展事件确认机制；认证、配额、限流、配置、上下文和内容策略错误不得自动重连。

## 主要导航

- `README.md`：仓库入口与基础说明。
- `AGENTS.md`：全仓协作规范、Web 会话状态与产品约束。
- `docs/platform-overview.md`：平台整体概览。
- `docs/getting-started.md`：本地开始使用说明。
- `docs/flowrun-openhands-runtime-design.md`：FlowRun Runtime 设计。
- `docs/flowrun-runtime-task-progress.md`：Runtime 重构切片进度与验证边界。
- `docs/deployment.md` 与 `docs/local-build-and-deploy.md`：部署和本地构建说明；仅在明确涉及部署时阅读并遵循。
- `apps/web/`：Web 前端实现；样式和交互变更需参考相邻的既有组件与测试。

## 验证与交付

- 按任务阶段和仓库约束运行允许的最窄检查；不要提前执行超出当前切片的集中式门禁。
- 提交前运行 `git diff --check`，检查 diff 只包含当前任务所需的内容，且没有临时文件、缓存、生成物或敏感信息。
- 提交必须是单一、可审计的变更单元；完成当前切片并提交后，不自动扩展到下一项工作。
- 对于平台资源操作，采用“读取 → 检查前置条件 → 最小写入 → 写后重新读取确认”的闭环；异步请求返回后读取同一资源，不要重复创建。
