# HST AI 提交标签节点结束门禁

## 目的与边界

此门禁确保一个 FlowWeave 节点在工作区内修改并提交代码后，所有**本次节点执行新增的、受控 Git 仓库提交**都带有精确标签：

```text
[HST_AI_Tag: AI_Generated]
```

它适配本机全局 Hook [`/Users/zhengmengen/.git-hooks/commit-msg`](/Users/zhengmengen/.git-hooks/commit-msg)，但比该 Hook 更严格：现有 Hook 也接受 `Manual` 和 `AI_Assisted`；本节点门禁只接受 `AI_Generated`。受控仓库范围仍与 Hook 一致：`origin` URL 包含 `gitlab.inzwc.com`、`bss-git.hszq8.com` 或 `internal-git.hszq8.com`。

不要把脚本配置成 FlowWeave `Hook` 能力；该能力创建入口已下线。将脚本作为版本化 `Skill` 的只读资源导入 Environment，并在节点的执行提示词中调用它。END Prompt Gate 只审核冻结的报告产物，不直接访问工作区或 Git。

## 节点输出字段

给目标节点增加一个必填输出字段：

| 字段 | 方向 | 类型 | 说明 |
| --- | --- | --- | --- |
| `git_tag_report` | OUTPUT | FILE | 本次节点 Git 标签审计的 UTF-8 JSON 报告 |

节点 Agent 应在 `/tmp` 保存临时基线和报告，再把报告作为 `git_tag_report` 产物上传。不要把基线或报告写进被审计仓库。

## 节点执行提示词附加段

将以下文字附加到会修改代码的节点执行提示词中。`/runtime/capabilities/<manifest-digest>/gates/hst-ai-tag/verify-hst-ai-tags-gate.py` 必须替换为当前 Environment 中该门禁包的实际冻结路径。

```text
Git 提交审计（强制）：

1. 在第一次修改任何代码或 Git 元数据之前，确定本节点的工作区根目录 WORKSPACE_ROOT，并执行：
   python3 /runtime/capabilities/<manifest-digest>/gates/hst-ai-tag/verify-hst-ai-tags-gate.py \
     snapshot --scope "$WORKSPACE_ROOT" --output /tmp/hst-ai-tag-baseline.json
2. 仅当确实需要提交时，所有由你创建的受控仓库提交信息必须包含精确标签
   `[HST_AI_Tag: AI_Generated]`。不要使用 Manual 或 AI_Assisted，也不要使用 --no-verify 绕过本机 Hook。
3. 完成所有代码修改、测试和提交后，执行：
   python3 /runtime/capabilities/<manifest-digest>/gates/hst-ai-tag/verify-hst-ai-tags-gate.py \
     verify --scope "$WORKSPACE_ROOT" --baseline /tmp/hst-ai-tag-baseline.json \
     --output /tmp/git-tag-report.json
4. 将 `/tmp/git-tag-report.json` 原样上传为本节点 `git_tag_report` 输出产物。不得伪造、删改或用摘要替代报告。
5. 只有报告中的 `decision` 为 `PASS` 才能宣布完成。若为 `FAIL`，修正未提交变更或提交信息后重跑审计；若为 `ERROR`，停止并报告无法审计的原因。无代码或无新增提交时也必须上传报告，脚本会给出 PASS/UNCHANGED 证据。
```

## END Prompt Gate 提示词

创建一个 END、PROMPT 类型的门禁，配置独立的 Gate Agent。将下面内容原样填写到 `config.prompt`：

```text
你正在审核“Git 提交标签审计”结束门禁。只能根据门禁上下文中冻结的输出产物判断；不得访问工作区、执行命令、猜测未提供的 Git 状态，或因节点的文字说明而放宽要求。

在 outputs 中找到 field_key 为 git_tag_report 的 FILE 产物，并读取其 review_preview 中的 UTF-8 JSON。若没有该产物、没有 TEXT 预览、JSON 无法解析、kind 不等于 flowweave_hst_ai_tag_report、schema_version 不等于 1，或报告被截断，必须 FAIL。

只有同时满足以下条件才能 PASS：
1. 报告 decision 严格等于 PASS；
2. policy.required_tag 严格等于 [HST_AI_Tag: AI_Generated]；
3. audit_errors 和 violations 都是空数组；
4. repositories 中不存在 UNCOMMITTED_CHANGES、TAG_VIOLATION、HISTORY_UNVERIFIABLE、UNVERIFIABLE_REPOSITORY、MISSING_REPOSITORY 或 ORIGIN_CHANGED 状态；
5. 每个 controlled_repository 为 true 且 state 为 COMPLIANT 的仓库中，所有 commits 条目的 status 都为 PASS；
6. 不把 SKIPPED_UNCONTROLLED_REPOSITORY 当成受控仓库合规证据，也不因为它存在而拒绝其它已合规仓库。

如果任一条件不满足，decision 为 FAIL，并在 reasons 中写明仓库路径、提交 ID（如有）和违反项。若报告本身不可读或结构不足以安全判断，decision 为 ERROR。evidence 只引用 Artifact ID、仓库路径和提交 ID；不要复制报告全文。返回且仅返回一个符合门禁契约的 JSON 对象。
```

建议 Gate 的 `timeout_seconds` 使用 300，并显式选择已经通过测试的独立模型配置。该 Gate 只对节点本次执行冻结，不会修改历史节点或历史提交。

## 脚本接口

脚本位于 [verify-hst-ai-tags-gate.py](/Users/zhengmengen/WorkSpace/FlowWeave/gates/hst-ai-tag/verify-hst-ai-tags-gate.py)。它只读 Git 数据；其基线记录每个仓库开始时的 `HEAD`，然后只检查从该 `HEAD` 到当前 `HEAD` 的新增提交。

```bash
# 节点开始前
python3 gates/hst-ai-tag/verify-hst-ai-tags-gate.py snapshot \
  --scope "$WORKSPACE_ROOT" --output /tmp/hst-ai-tag-baseline.json

# 节点结束时；默认无论 PASS/FAIL 都返回 0，以便 Agent 上传完整报告
python3 gates/hst-ai-tag/verify-hst-ai-tags-gate.py verify \
  --scope "$WORKSPACE_ROOT" --baseline /tmp/hst-ai-tag-baseline.json \
  --output /tmp/git-tag-report.json

# 本地 CI 如需让 FAIL/ERROR 直接失败，可加 --strict
python3 gates/hst-ai-tag/verify-hst-ai-tags-gate.py verify \
  --scope "$WORKSPACE_ROOT" --baseline /tmp/hst-ai-tag-baseline.json --strict
```

脚本在以下情形按失败关闭：工作区存在未提交或未跟踪变更、基线后的受控提交没有精确标签、历史被改写/切换而无法确定提交范围、审计范围中出现基线后新增或消失的仓库，或 `origin` 在基线后被更换。非受控仓库会被列出为 `SKIPPED_UNCONTROLLED_REPOSITORY`，以保持与现有本机 Hook 的域名策略一致。
