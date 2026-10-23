---
name: flowweave-agent-schedules
description: 创建、查询、更新、暂停、恢复、手动触发或删除 FlowWeave Agent 自动化模板，并读取其运行 occurrence；FlowRun 调度转 flowweave-schedules。
---

# FlowWeave Agent 定时自动化

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 只处理顶层 Agent Workspace 自动化模板，不处理 FlowRun 周期调度；后者转 `flowweave-schedules`。

## 模板边界

一个自动化模板保存业务提示词、按北京时间（`Asia/Shanghai`）填写的标准五段 Cron、模型配置、工作区模式和会话模式。它不绑定或复制任意来源会话。

- `AUTOMATION` 工作区模式为默认值：平台创建一个固定的模板专属工作区，后续每次运行持续使用它。
- `EXISTING` 工作区模式绑定用户选择的已有工作区；定时详情仍只显示该模板的运行会话和关联产物，不能把它当作该物理工作区的完整视图。
- `NEW` 会话模式为默认值：每次触发创建完整独立 Agent 会话。
- `CONTINUE` 会话模式由模板持有一个平台管理的持续会话，后续 occurrence 向该会话追加自动化消息。

平台在模板创建时自动写入配置与记忆文件。用户和本 Skill **只能描述业务目标**，不得手写或伪造平台前缀、任务 ID、配置文件路径、记忆文件路径或上次运行信息。

每次投递会在业务提示词开头自动加入：

```text
Automation: <模板名称>
Automation ID: <平台生成 ID>
Automation memory: <平台管理的 memory.md>
Last run: <平台状态>
```

随后平台加入“仅在当前自动化工作区内工作”“读取并更新配置与记忆文件”“产物必须写入工作区”的固定约束。

## 生成业务提示词草案

接到用户的自然语言自动化需求时，先将其整理为一份**仅包含业务内容**的可编辑提示词：

1. 明确本次任务目标、输入、处理步骤和最终摘要；
2. 指明需要生成、检查或更新的业务产物及其相对路径；
3. 对数据证据、输出语言、条数、格式和异常说明提出具体要求；
4. 明确用户禁止的业务修改、外部访问或资源使用；
5. 不要重复 Automation、工作区、memory、Last run 或平台安全前缀；这些由平台注入。

例如用户要求“每天生成项目日报”时，业务提示词可要求读取已有报告、生成 `reports/latest.html`、列出管理摘要并验证产物存在；不要写入机器绝对路径，不要要求绕过登录、限制、付费墙、robots 或限额。

## 调度时间

`cron_expression` 的顺序为分、时、日、月、周，按 `Asia/Shanghai` 匹配；每天北京时间 10:00 填 `0 10 * * *`，工作日 09:00 填 `0 9 * * 1-5`，不要先减 8 小时换算成 UTC。读取 `next_run_at`、`scheduled_for` 等绝对时间时按其时区解析，再转为北京时间核对。已有任务已由平台迁移为北京时间表达式，更新时直接使用当前读取值，不重复加 8 小时。

## 操作闭环

1. 读取 Workspace、工作区目录、模型提供商和已有自动化，确认真实 ID：

   ```bash
   flowweave agent workspace <workspace-id>
   flowweave agent work-directories <workspace-id>
   flowweave resource model-providers list
   flowweave agent schedules <workspace-id>
   ```

2. 生成业务提示词草案后，创建请求必须包含：`name`、`prompt`、`cron_expression`、`workspace_mode`、`conversation_mode`、`model_provider_id`、`model_name` 和可选 `reasoning_effort`。仅在 `workspace_mode=EXISTING` 时填写 `work_directory_id`。先审阅请求：

   ```bash
   flowweave agent schedule-create <workspace-id> \
     --data-file ./.tmp/agent-schedule.json --dry-run
   ```

3. 更新、暂停或恢复前重新读取任务，以响应中的 `row_version` 防止覆盖并发修改：

   ```bash
   flowweave agent schedule-update <workspace-id> <schedule-id> \
     --data-file ./.tmp/agent-schedule.json --dry-run
   flowweave agent schedule-pause <workspace-id> <schedule-id> \
     --expected-row-version <row-version> --dry-run
   flowweave agent schedule-resume <workspace-id> <schedule-id> \
     --expected-row-version <row-version> --dry-run
   ```

4. 运行状态和会话 binding ID 只以 occurrence 为准；仅使用其返回 ID 打开产生或续用的会话：

   ```bash
   flowweave agent schedule-occurrences <workspace-id> <schedule-id>
   ```

## 状态变更

`agent schedule-trigger` 和 `agent schedule-delete` 会改变平台状态。执行前必须取得用户对精确 schedule ID 的明确授权，并先通过 `--dry-run` 核对 URL、范围和请求体。异步 trigger 返回后读取 occurrence，而不是重复触发。

```bash
flowweave agent schedule-trigger <workspace-id> <schedule-id> --dry-run
flowweave agent schedule-delete <workspace-id> <schedule-id> --dry-run
```
