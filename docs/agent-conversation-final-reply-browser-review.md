# Agent 已有会话最终回复未实时显示：浏览器复查手册

## 目标

复查以下现象：在**已有会话**中继续发送一条消息后，页面仍展示“正在思考”或其他运行状态，最终回复只有刷新页面才出现。

本手册要回答两个问题：

1. 最终回复是否已经被 Runtime/服务端正式事件持久化？
2. 如果已经持久化，浏览器为什么没有把它合并到当前会话画面？

> 最终回复正文只应来自正式 `MESSAGE` 事件；不要以 WebSocket `delta` 文本是否存在来判断回复已经完成。

---

## 复查前准备

### 1. 使用稳定的测试条件

- 使用可写的已有会话，而不是新建会话的首条消息。
- 选择能产生数秒过程事件的请求，例如：

  ```text
  请先读取项目根目录下的 package.json 和 AGENTS.md，说明项目使用的包管理器，并列出两个关键脚本。完成后给出简短结论。
  ```

- 不要在消息中包含真实凭据、个人数据或生产敏感内容。
- 建议准备一个新的测试会话，避免旧历史的活动事件影响观察。

### 2. 打开浏览器开发者工具

在发送任何消息之前完成下列设置：

1. 打开 **DevTools → Network**。
2. 勾选 **Preserve log**（保留日志）。
3. 勾选 **Disable cache**（仅 DevTools 打开时有效）。
4. 在 Network 类型过滤中分别查看：
   - `Fetch/XHR`：正式事件 REST 读取和消息提交；
   - `WS`：会话 WebSocket 帧。
5. 记录当前页面地址。已有会话地址通常形如：

   ```text
   /agent/conversations/<bindingId>
   ```

> 不要先刷新页面；刷新是本次对比实验的最后一步。

---

## 需要观察的请求

以下路径中的 `<workspaceId>` 和 `<bindingId>` 均以 Network 中实际请求为准。

| 用途 | 请求/连接 | 重点字段 |
| --- | --- | --- |
| 提交已有会话消息 | `POST /api/v1/agent-workspaces/<workspaceId>/conversations/<bindingId>/messages` | `202`、响应体 `accepted`、`cursor` |
| 订阅实时事件 | `WS /api/v1/agent-workspaces/<workspaceId>/conversations/<bindingId>/stream` | Frames 中的 `event`、`durable_cursor`、`stream_closed` |
| 读取正式事件 | `GET /api/v1/agent-workspaces/<workspaceId>/conversations/<bindingId>/events` | `events`、`next_cursor`、`result` |
| 初次/刷新 hydration | `GET .../conversations/<bindingId>/hydration` | 刷新后最终 `MESSAGE` 是否出现 |
| Runtime 交互状态 | `GET .../conversations/<bindingId>/input-readiness` | `ready`、`execution_status` |

正式事件读取可能包含：

- `?cursor=<cursor>`：增量读取；
- 无 `cursor`：读取最新有限事件窗口（本次最关键）；
- `diagnostic_trigger=...`：由前端回补触发时可能存在，例如 `scheduled`、`foreground`、`stream_closed` 或 `stream_reconnect`。

---

## A. 基线：先验证一次正常的已有会话续发

这一步用于确认当前环境的常规链路正常，并获得对照样本。

### 操作

1. 在已有会话中发送测试消息。
2. 立即在 Network 找到 `POST .../messages`。
3. 记录响应中的：
   - HTTP 状态；
   - `accepted`；
   - `cursor`（这是本次正式用户事件的身份）。
4. 在同一会话的 WS 连接打开 **Messages/Frames**。
5. 等待过程事件和最终回复出现，不刷新页面。
6. 在页面中确认：
   - 用户气泡出现一次；
   - 过程状态可以变化；
   - 最终 assistant 回复显示；
   - 发送按钮从暂停/运行态恢复为可发送。

### 正常样本中应能看到

#### WebSocket

过程事件与最终事件通常具有如下结构（字段可能更多）：

```json
{
  "type": "event",
  "event": {
    "id": "...",
    "event_type": "TOOL_CALL",
    "payload": { "parent_id": "..." }
  }
}
```

最终回复应是正式事件，而不是 `delta`：

```json
{
  "type": "event",
  "event": {
    "id": "...",
    "event_type": "MESSAGE",
    "payload": {
      "source": "agent",
      "parent_id": "<本轮用户事件或其后代>",
      "content": "..."
    }
  }
}
```

连接也可能出现以下帧：

```json
{ "type": "durable_cursor", "seq": 123 }
{ "type": "stream_closed", "item_id": "..." }
```

`stream_closed` 是回补触发线索之一，但不是最终回复正文。

#### REST `/events`

最终 `GET .../events` 响应的 `events` 数组中，应存在：

```json
{
  "id": "...",
  "event_type": "MESSAGE",
  "payload": {
    "source": "agent",
    "parent_id": "...",
    "content": "..."
  }
}
```

它必须属于本轮提交的 `cursor` 对应分支。可根据 `parent_id` 沿事件父链回溯确认；不要只按时间或正文相似度判断。

---

## B. 复现：已有会话继续发送后不刷新等待

### 操作

1. 保持 Network 日志不清空，继续在**同一个已有会话**发送第二条消息。
2. 再次记录 `POST .../messages` 响应的 `cursor`，记为 `C2`。
3. 不刷新、不切换会话、不点击暂停/继续。
4. 等待到以下任一条件：
   - 页面显示最终回复；或
   - 页面持续显示“正在思考/运行中”超过预期完成时间；建议至少多等待 **15–30 秒**；或
   - 确认 Runtime 侧任务已经完成。
5. 一旦出现“页面未显示回复”的现象，**先保留当前 Network 记录，不要马上刷新**，按下一节抓取证据。

### 记录 UI 状态

截图或写下：

- 最后一条可见过程事件及时间；
- 是否仍显示“正在思考”“正在运行”“连接恢复中”或暂停按钮；
- 是否看到最终 assistant 气泡；
- 该会话是否仍有侧栏运行指示；
- 是否可继续发送消息。

---

## C. 复现现场的证据采集（刷新前）

### 1. 检查 WebSocket 最后一批帧

在对应会话的 WS 条目中，检查最后 20–50 条帧并记录：

1. 是否持续有 `TOOL_CALL`、`TOOL_RESULT`、`THOUGHT` 等过程 `event`；
2. 是否收到本轮 `C2` 分支下的 assistant `MESSAGE`；
3. 是否收到 `COMPLETED + FinishAction` 或 `ERROR`；
4. 是否收到 `stream_closed`；
5. 连接状态是否仍为 Open/101，还是发生关闭/重连。

**关键模式 1：最符合当前怀疑的缺口**

- 有过程事件；
- 没有最终 assistant `MESSAGE`；
- 没有 `stream_closed`；
- WebSocket 仍是打开状态；
- 页面仍处于运行态。

这表示“连接表面正常，但终态投影没有可靠到达浏览器”。

### 2. 检查页面是否仍读取正式事件

在 Fetch/XHR 中筛选：

```text
/events
```

重点看卡住后的 15–30 秒内：

- 是否还有 `GET .../events`；
- 请求是否为无 `cursor` 的最新窗口读取；
- 若有 `cursor`，它是否只是读取旧增量；
- 是否存在 `diagnostic_trigger`；
- 是否返回 200；
- 是否返回 503/409/超时等错误。

**重点判定：**

| 现场现象 | 初步判断 |
| --- | --- |
| 卡住后不再有 `/events` 请求 | 前端回补调度停止，高优先级怀疑点 |
| 仍有 `/events`，且响应已含本轮 agent `MESSAGE`，但页面没显示 | 前端事件合并/分支关联/渲染过滤问题 |
| 仍有 `/events`，但响应不含最终 `MESSAGE` | 服务端尚未可读，或读取窗口/游标不正确 |
| `/events` 返回 5xx、503、409 或超时 | Runtime 读取不可用，应同时检查对应错误响应和服务端诊断 |

### 3. 检查 `input-readiness`

找到同一时间段的：

```text
GET .../input-readiness
```

记录响应中的：

```json
{ "ready": true, "execution_status": "idle" }
```

或：

```json
{ "ready": false, "execution_status": "running" }
```

**重点组合：**

- `ready: true` / `idle`，但 UI 仍“正在思考”，且未继续 `/events` 回补：非常符合本问题的竞态；
- `running`，但 `/events` 已包含最终 agent `MESSAGE`：浏览器渲染或状态归并问题；
- `running`，且 `/events` 也没有最终消息：Runtime 可能仍未完成，不能仅凭 UI 认定缺陷。

---

## D. 刷新对比（只在 C 完成后执行）

### 操作

1. 记下刷新前最后一条 `/events` 响应的时间和内容摘要。
2. 刷新当前会话页面。
3. 在 Preserve log 保持开启的情况下，查看刷新后新增的 `/hydration` 与 `/events` 响应。
4. 确认最终 assistant 回复是否出现。

### 判定矩阵

| 刷新前 | 刷新后 | 结论 |
| --- | --- | --- |
| 页面无回复；刷新前 `/events` 已有 agent `MESSAGE` | 页面显示该回复 | 前端实时事件合并/渲染故障 |
| 页面无回复；刷新前没有后续 `/events` | hydration 中有 agent `MESSAGE`，页面显示 | **前端回补停止**；最符合当前根因假设 |
| 页面无回复；刷新前 `/events` 无 agent `MESSAGE` | hydration 也无 agent `MESSAGE` | Runtime/服务端尚未产出或尚不可读，不能归因为前端遗漏 |
| 页面无回复；刷新前 `/events` 返回错误 | 刷新后恢复并显示 | 运行时读取短暂不可用；需结合服务端诊断进一步判断 |

---

## 建议保存的最小证据包

不要导出或共享包含敏感提示词、附件内容、Cookie、Authorization 请求头或完整 HAR 的原始文件。应先脱敏。

每次复现至少保存以下内容：

1. **复现说明**
   - 时间；
   - 会话页面路径中的 binding ID（可按团队规则部分遮盖）；
   - 第二条消息 POST 返回的 `cursor`；
   - 是否为已有会话续发。
2. **刷新前截图**
   - 运行态/“正在思考”；
   - 最后一个过程事件；
   - 缺少最终回复的会话区。
3. **WS 帧摘要**
   - 最后一个过程事件；
   - 是否有 agent `MESSAGE`；
   - 是否有 `stream_closed`；
   - socket 是否断开/重连。
4. **刷新前 `/events` 摘要**
   - 请求时间、查询参数、状态码；
   - 是否仍发起 latest-read；
   - 是否含本轮 agent `MESSAGE`。
5. **`input-readiness` 摘要**
   - `ready`、`execution_status`。
6. **刷新后 hydration/events 摘要**
   - 是否首次看到本轮 agent `MESSAGE`；
   - 页面是否立即出现相同最终回复。

建议使用如下记录模板：

```text
时间：
已有会话 binding：
本轮 POST cursor：

刷新前 UI：
- 可见状态：
- 最后过程事件：
- 最终回复是否可见：否

WS：
- 最后过程 event：
- assistant MESSAGE：有 / 无
- stream_closed：有 / 无
- socket 状态：open / closed / reconnecting

/events（刷新前）：
- 最后请求时间：
- 是否有 latest-read（无 cursor）：是 / 否
- 状态码：
- 是否含本轮 assistant MESSAGE：是 / 否

/input-readiness：
- ready：
- execution_status：

刷新后：
- hydration 或 events 是否含 assistant MESSAGE：是 / 否
- 页面是否显示最终回复：是 / 否

初步结论：
```

---

## 面向 Codex 浏览器操作者的执行顺序

可直接按下列顺序操作：

1. 进入已有会话，打开 DevTools Network，设置 Preserve log 和 Disable cache。
2. 发送一条会产生过程事件的消息，记录 POST 返回 cursor。
3. 不刷新，等待最终结果。
4. 若正常完成，记录一组正常 WS + `/events` 对照。
5. 在同一会话继续发送第二条消息，记录新 cursor。
6. 出现“运行中但无最终回复”时，先采集 WS、`/events`、`input-readiness`。
7. 特别确认：刷新前 `/events` 是否仍在请求，及其响应是否已经有 agent `MESSAGE`。
8. 最后刷新页面，比较 hydration/最新 events 是否首次暴露该 agent `MESSAGE`。
9. 按上文判定矩阵输出结论；不要以 delta、单纯 spinner 或网络连接 Open 状态单独下结论。

---

## 关联实现位置（供代码交叉核对）

- 前端流事件与终态处理：
  `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx:6544-6585`
- 前端定期正式事件回补：
  `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx:6131-6156`
- 发送已有会话消息后的 cursor/确认逻辑：
  `apps/web/src/components/agent-session/AgentSessionWorkbench.tsx:7093-7141`
- WebSocket 客户端解析：
  `apps/web/src/api/client.ts:953-1014`
- 后端 WebSocket 会话流端点：
  `services/platform/src/flowweave/modules/agent_workspaces/presentation/router.py:1561-1602`
- 后端正式事件 REST 端点：
  `services/platform/src/flowweave/modules/agent_workspaces/presentation/router.py:971-1009`
- Runtime 对用户可见正式事件的流投影：
  `services/platform/src/flowweave/runtime/openhands.py:4759-4801`
