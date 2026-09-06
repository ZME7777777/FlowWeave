---
name: flowweave-capabilities
description: 导入、发布、查询或管理 FlowWeave Skill、MCP、Plugin、Context 等能力及其受治理版本；不直接写入 Runtime。
---

# FlowWeave 能力仓库

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 管理平台中受版本治理的能力，不是把文件直接安装到某台 Runtime 的说明。

## 对象与前置条件

能力可以是 Skill、MCP、Plugin、Context 与 Agent Definition 等类型。平台导入后产生可追溯的版本、digest/blob/hash；节点、环境或 Agent Workspace 只能使用平台允许的版本。能力文件在提交前必须通过验证，且不应包含明文密钥。Hook 已从产品入口下线：历史记录可以读取，但新的 Hook 导入会被服务端拒绝，不能尝试绕过该限制。

先读取已有能力和线上 OpenAPI，确定文件格式、能力类型和请求字段：

```bash
flowweave capability list
flowweave openapi --paths
```

## 导入闭环

导入采用两阶段流程，适合让用户先审阅校验结果：

```bash
flowweave capability validate --type SKILL --file ./skill.zip
# 从返回值取得 import_token；确认校验报告与目标类型后再提交
flowweave capability commit --import-token <import-token>
flowweave capability list
```

只有用户明确同意一次性导入时才使用 `flowweave capability import --type <TYPE> --file <FILE>`。提交后读取能力列表/详情，记录返回的能力 ID、版本、状态和 digest；后续绑定时始终使用平台返回的版本身份。

## 深度能力操作

MCP 探测、OAuth secret reference、Plugin source resolve/publish、Context source 修改、Capability collection 等属于同一治理域。OpenHands Marketplace 可通过 CLI 进行受治理闭环：目录浏览会先由服务端把公开仓库 HEAD 解析为完整 commit；必须使用返回的 `source`、`commit`、`repo_path` 与插件名创建解析，再轮询同一 resolution 并以其当前 `state_version` 发布。不要以浮动分支、浏览器下载或直接复制到 Runtime 替代该路径。

```bash
flowweave capability marketplace
flowweave capability marketplace-resolve --data-file ./marketplace-plugin.json --dry-run
flowweave capability marketplace-resolve --data-file ./marketplace-plugin.json
flowweave capability plugin-resolution <resolution-id>
flowweave capability plugin-publish <resolution-id> \
  --data '{"expected_state_version": 1}' --dry-run
```

自定义受信任 Marketplace 先使用 `capability marketplace-preview --data-file ./marketplace.json` 审阅目录。异步 resolve 返回 `202` 时读取同一个 resolution ID 的状态，不要重复提交。OAuth 只经平台授权引用管理；不把 token 放进请求样例、文件或日志。

Agent Definition 仅接受 UTF-8 `.md`/`.markdown` 文件：必须包含 YAML frontmatter 和 Markdown 正文，`<example>…</example>` 用于触发示例；正文冻结为系统提示词。不要提交 JSON、分离脚本、嵌套 Skill/MCP/Hook 配置、显式模型或非治理权限／压缩设置。平台会将未声明的权限和压缩分别冻结为 `never_confirm` 与 `NoOpCondenser`。

要把能力绑定到 Agent Workspace，转 `flowweave-agent-workspace`；要让能力进入可运行的环境，先确保版本可用，再转 `flowweave-environments`。不要直接复制文件到 Docker、OpenHands HOME 或项目目录来伪造能力已发布。

## 变更与清理

更新 source、删除能力或撤销 OAuth reference 前，先读取能力版本和引用资源。运行或环境快照仍引用的版本应保留，除非用户明确要求处理这些引用；删除成功后再次列出或读取目标确认结果。
