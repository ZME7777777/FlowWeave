---
name: flowweave-environments
description: 创建、配置、发布或清理 FlowWeave 终端环境及其不可变版本时使用。
---

# FlowWeave 终端环境

用 `environment list` 或 `environment get <id>` 先确认目标。创建环境使用 `environment create --data '{"name":"...","description":"..."}'`。创建配置会话使用 `environment setup <environment-id>`，发布该会话使用 `environment publish <setup-session-id>`，停止会话使用 `environment stop <setup-session-id>`。

FlowRun 只能绑定已经 READY 的不可变 Environment Version。删除环境或版本前必须确认没有用户要求保留的 Run/Snapshot 引用；版本删除用 `environment version-delete <environment-id> --version <version-id>`。
