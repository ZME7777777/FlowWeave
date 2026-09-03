---
name: flowweave-environments
description: 创建、配置、发布或清理 FlowWeave 终端环境及其不可变版本；FlowRun 选择环境版本转 flowweave-runs。
---

# FlowWeave 终端环境

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 处理 Environment 与其不可变版本；Flow 本身不绑定环境，运行时选择版本由 FlowRun 处理。

## 生命周期与关系

Environment 是可维护的环境定义。通过 Setup Session 在受控终端中配置，发布后产生不可变的 Environment Version。只有状态为 `READY`、且平台已冻结 digest/manifest 的版本，才能用于创建 FlowRun。后续需要修改时，从已有版本创建新的 Setup Session，再发布新版本；不能编辑已发布版本。

## 创建、配置、发布

1. 读取当前环境、版本与状态，避免以名称猜版本：

   ```bash
   flowweave environment list
   flowweave environment get <environment-id>
   ```

2. 创建/修改环境定义。具体字段遵循在线 `TerminalEnvironmentWrite` schema：

   ```bash
   flowweave environment create --data-file ./environment.json --dry-run
   flowweave environment create --data-file ./environment.json
   ```

3. 创建 Setup Session；若需以某版本为基线，在 OpenAPI schema 指定其真实 `base_version_id`。终端交互走返回 session 的 WebSocket 路径，不直接连接容器。

   ```bash
   flowweave environment setup <environment-id> --data-file ./setup.json
   flowweave ws /environment-setup-sessions/<setup-session-id>/terminal
   ```

4. 完成配置后发布，重新读取 Environment，确认新版本为 `READY`，并保存**返回的 version ID**供 `flowweave run start --environment-version` 使用：

   ```bash
   flowweave environment publish <setup-session-id>
   flowweave environment get <environment-id>
   ```

## 失败、停止与删除

Setup Session 未完成或不再需要时可 `flowweave environment stop <setup-session-id>`；停止前确认会丢弃的是正确会话。发布失败时先读取 session/环境状态和平台事件，再修改配置后新建会话，不对底层容器做人工修复。

删除环境或版本前必须读取其所有版本与关联 Run/Snapshot。运行已经冻结引用的版本不应删除；仅在用户明确授权、ID 精确且没有需保留引用时使用 `environment delete` 或 `environment version-delete <environment-id> --version <version-id>`。
