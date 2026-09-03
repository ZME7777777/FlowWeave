# Exception Collection Document Templates

飞书只保存阶段一/二事实。窗口父文档不含任何处理章节、修复状态列或阶段三进度；指纹子文档严格止于第七章。修复过程、跳过选择、代码、commit 和 push 只写本地 `TASK.md`。

## Window parent document

```xml
<title>MMDD-MMDD</title>
<callout emoji="📌"><p>应用：{app}；环境：HK；窗口：{start} - {end}（Asia/Shanghai）。</p></callout>
<table>
  <thead><tr><th>阶段</th><th>状态</th><th>证据/说明</th></tr></thead>
  <tbody>
    <tr><td>阶段一：日志收集与闭合</td><td>{完成/未完成}</td><td>{total/bucket/other 等式}</td></tr>
    <tr><td>阶段二：链路上下文调查</td><td>{完成/未完成}</td><td>{完成指纹数/总指纹数}</td></tr>
  </tbody>
</table>
<h1>一、计数闭合</h1>
<p>ERROR total={total}；logger bucket sum={bucket_sum}；sum_other={sum_other}；classified={classified}；remainder={remainder}。</p>
<p>非 ERROR Exception total={non_error_total}，与 ERROR 按 ES _id 去重后独立统计。</p>
<h1>二、异常指纹总表</h1>
<table>
  <thead><tr><th>ID</th><th>精确数</th><th>endpoint/任务</th><th>root msg</th><th>调查状态</th><th>详细子文档</th></tr></thead>
  <tbody><tr><td>F001</td><td>{count}</td><td>{endpoint}</td><td>{root_msg}</td><td>{确认/高概率/证据耗尽}</td><td>{child_link}</td></tr></tbody>
</table>
<h1>三、公共时间线与依赖</h1>
<p>{跨指纹公共事实；不得替代子文档直接证据。}</p>
<h1>四、完成审计</h1>
<p>{计数闭合、子文档数量、调查状态、回读和脱敏检查。修复进度见本地 TASK.md，不写入本文档。}</p>
```

无日志证据时仍写父文档，记录双轨精确查询条件与 0 条证据，不写阶段三。

## Fingerprint child document

稳定标题：`[F001] endpoint-or-task - root-msg-summary`

```xml
<title>[F001] endpoint-or-task - root-msg-summary</title>
<table><tbody>
  <tr><td>应用/环境/窗口</td><td>{app} / HK / {start} - {end}</td></tr>
  <tr><td>指纹键</td><td>{logger + endpoint/task + root msg + first business frame}</td></tr>
  <tr><td>精确数量</td><td>{count}</td></tr>
  <tr><td>阶段二状态</td><td>{确认/高概率/证据耗尽}</td></tr>
</tbody></table>
<h1>一、问题详情</h1>
<p>{现象、时间分布、入口/任务、fast-throw 边界。}</p>
<h1>二、完整脱敏异常原文</h1>
<pre lang="text"><code>{timestamp/app/pod/logger/thread/reqId/full msg/full chain/full available stack}</code></pre>
<h1>三、检索与调查尝试</h1>
<table>
  <thead><tr><th>证据源</th><th>查询条件</th><th>结果</th><th>结论/边界</th></tr></thead>
  <tbody>
    <tr><td>ES service</td><td>{window/host/fingerprint}</td><td>{count}</td><td>{...}</td></tr>
    <tr><td>buzz-access</td><td>{endpoint then reqId}</td><td>{count}</td><td>{...}</td></tr>
    <tr><td>下游 service/access</td><td>{service/method/reqId}</td><td>{count}</td><td>{...}</td></tr>
    <tr><td>SkyWalking</td><td>{traceId or service+endpoint+minute}</td><td>{spans/0}</td><td>{...}</td></tr>
    <tr><td>同簇/归档</td><td>{conditions}</td><td>{result}</td><td>{direct/cluster/unavailable}</td></tr>
  </tbody>
</table>
<h1>四、时间线</h1>
<table><thead><tr><th>时间</th><th>应用</th><th>动作/span</th><th>结果/耗时</th></tr></thead>
<tbody><tr><td>{time}</td><td>{service}</td><td>{method}</td><td>{result}</td></tr></tbody></table>
<h1>五、reqId 与上下游链路</h1>
<p>{interface → consumer → provider → dependency；标明成功、失败、空 model、错误码和二次异常。}</p>
<h1>六、SkyWalking 证据</h1>
<p>{trace、错误 span、最深异常、慢节点；无结果时写完整查询条件。}</p>
<h1>七、阶段二结论</h1>
<p>置信度：{确认/高概率/证据耗尽}。{直接触发、故障边界、上下游返回和证据缺口。}</p>
```

## Content rules

- 不同 endpoint、root msg、业务错误码或首业务栈必须拆分。
- 完整日志只脱敏值，不删除异常链和已有堆栈。
- 同簇、跨日和当前 HEAD 源码明确标注证据关系与版本边界。
- 父文档和子文档不得包含“修复主状态、是否解决、环境清理、处理选择、分支、commit、push、阶段三、第八章”等修复过程字段。
- 若更新旧批次，先批量定位该批次父文档和全部指纹中的旧阶段三行、修复列、第八章与顶部修复状态块，再集中删除并逐篇回读；不得把清理动作拆成逐项状态回写。
