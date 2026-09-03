---
name: flowweave-capabilities
description: 导入、发布、查询或管理 FlowWeave Skill、MCP、Plugin、Context 等能力时使用；不直接写入 Runtime。
---

# FlowWeave 能力仓库

先用 `flowweave capability list` 查看已发布能力。导入本地能力文件使用两阶段命令：

`flowweave capability validate --type SKILL --file ./skill.zip` 只校验并返回导入令牌；确认后执行 `flowweave capability commit --import-token <token>`。需要一次完成时使用 `capability import`。

能力类型、文件格式和请求体以 `flowweave openapi --paths` 及服务端响应为准。不要绕过 FlowWeave 直接把 Skill/MCP/Plugin 写入 OpenHands Runtime；固定版本、digest 与加载边界由平台治理。
