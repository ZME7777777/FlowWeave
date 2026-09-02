import { Check, ChevronDown, ChevronRight, CircleAlert, Copy, ExternalLink, FileText, GitFork, Link, LoaderCircle, PanelRightOpen, Pencil, Sparkles, SquareTerminal, Wrench } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentAttachment, OpenHandsConversationEvent } from '../types';
import './conversation-surface.css';

type ItemKind = 'user' | 'assistant' | 'thought' | 'tool' | 'error' | 'condensation';

interface Item {
  event: OpenHandsConversationEvent;
  kind: ItemKind;
  title: string;
  content: string;
}

interface Turn {
  id: string;
  user?: Item;
  assistant?: Item;
  activity: Item[];
}

interface UserMessageNavigationItem {
  id: string;
  content: string;
}

interface ActivityEntry {
  id: string;
  item: Item;
  action?: Item;
  results: Item[];
}

type TurnProcessBlock =
  | { kind: 'activity'; id: string; items: Item[]; startedAt?: number; finishedAt?: number; active: boolean }
  | { kind: 'condensation'; id: string; items: Item[] };

function eventAttachments(event: OpenHandsConversationEvent): AgentAttachment[] {
  return Array.isArray(event.payload.attachments) ? event.payload.attachments : [];
}

function attachmentSize(bytes: number): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function MessageAttachments({ attachments, onOpen }: {
  attachments: AgentAttachment[];
  onOpen?: (attachment: AgentAttachment) => void;
}) {
  if (!attachments.length) return null;
  return <div className="conversation-message-attachments" aria-label="消息附件">
    {attachments.map(attachment => <button
      type="button"
      key={attachment.path}
      className="conversation-message-attachment"
      title={`查看附件：${attachment.filename}`}
      onClick={() => onOpen?.(attachment)}
    >
      <FileText size={16}/><span><b>{attachment.filename}</b><small>{attachment.mime_type || '文件'}{attachmentSize(attachment.byte_size) ? ` · ${attachmentSize(attachment.byte_size)}` : ''}</small></span><PanelRightOpen size={13}/>
    </button>)}
  </div>;
}

function MarkdownImage({ src, alt, ...props }: ComponentPropsWithoutRef<'img'>) {
  const [failed, setFailed] = useState(false);
  const safeSource = typeof src === 'string' && /^(?:https?:|data:image\/|blob:|\/)/i.test(src);
  if (!safeSource || failed) {
    return <span className="conversation-image-unavailable" role="status">
      <b>{alt || '图片无法显示'}</b>
      {safeSource
        ? <a href={src} target="_blank" rel="noreferrer">在新窗口打开图片</a>
        : <small>图片地址无效</small>}
    </span>;
  }
  return <img {...props} className={`conversation-markdown-image${props.className ? ` ${props.className}` : ''}`} src={src} alt={alt ?? ''} onError={() => setFailed(true)}/>;
}

function MarkdownLink({ href, ...props }: ComponentPropsWithoutRef<'a'>) {
  const isExternal = typeof href === 'string' && /^(?:https?:\/\/|mailto:)/i.test(href);
  return <a {...props} href={href} {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}/>;
}

function MessageMarkdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink, img: MarkdownImage }}>{children}</ReactMarkdown>;
}

interface CandidateOutput { fieldKey: string; artifactType: 'URL' | 'FILE'; value: string }

function candidateOutputs(content: string): CandidateOutput[] | undefined {
  let raw = content.trim();
  if (raw.startsWith('```')) raw = raw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  let value: unknown;
  try { value = JSON.parse(raw); } catch { return undefined; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const outputs = (value as Record<string, unknown>).outputs;
  if (!outputs || typeof outputs !== 'object' || Array.isArray(outputs)) return undefined;
  const parsed: CandidateOutput[] = [];
  for (const [fieldKey, rawOutput] of Object.entries(outputs)) {
    if (!rawOutput || typeof rawOutput !== 'object' || Array.isArray(rawOutput)) return undefined;
    const output = rawOutput as Record<string, unknown>;
    const artifactType = output.artifact_type;
    const outputValue = artifactType === 'URL' ? output.uri : artifactType === 'FILE' ? output.path : undefined;
    if ((artifactType !== 'URL' && artifactType !== 'FILE') || typeof outputValue !== 'string' || !outputValue.trim()) return undefined;
    if (artifactType === 'URL') {
      try { if (!['http:', 'https:'].includes(new URL(outputValue).protocol)) return undefined; } catch { return undefined; }
    }
    parsed.push({ fieldKey, artifactType, value: outputValue.trim() });
  }
  return parsed.length ? parsed : undefined;
}

function CandidateOutputReply({ outputs }: { outputs: CandidateOutput[] }) {
  return <section className="conversation-candidate-outputs" aria-label="Agent 候选输出"><header><b>Agent 已提交 {outputs.length} 个候选输出</b><small>等待平台校验并登记为正式 Artifact；文件路径不会作为下载地址公开。</small></header><div>{outputs.map(output => <article key={output.fieldKey}>{output.artifactType === 'URL' ? <Link size={15}/> : <FileText size={15}/>}<span><b>{output.fieldKey}</b><small>{output.artifactType === 'URL' ? 'URL 候选产物' : '文件候选产物'}</small><p>{output.artifactType === 'URL' ? output.value : output.value.split('/').at(-1)}</p></span>{output.artifactType === 'URL' && <a href={output.value} target="_blank" rel="noopener noreferrer"><ExternalLink size={13}/>打开</a>}</article>)}</div><p>只有右侧“输出”页出现版本、哈希、预览或下载入口后，才表示产物已正式冻结。</p></section>;
}

function itemsFor(event: OpenHandsConversationEvent): Item[] {
  const content = typeof event.payload.content === 'string' ? event.payload.content : '';
  const thought = typeof event.payload.thought === 'string' ? event.payload.thought : '';
  const eventName = String(event.payload.event_name || event.event_type);
  if (event.event_type === 'MESSAGE') {
    const source = String(event.payload.source ?? '').toLowerCase();
    const isUser = source === 'user' || source === 'human';
    const displayContent = typeof event.payload.display_content === 'string'
      ? event.payload.display_content
      : content;
    return [{ event, kind: isUser ? 'user' : 'assistant', title: '', content: isUser ? displayContent : content }];
  }
  if (event.event_type === 'THOUGHT') return [{ event, kind: 'thought', title: '分析', content: thought || content }];
  if (event.event_type === 'CONDENSATION_REQUESTED') return [{ event, kind: 'condensation', title: '正在自动压缩上下文', content: '' }];
  if (event.event_type === 'CONDENSATION_COMPLETED') return [{ event, kind: 'condensation', title: '已自动压缩上下文', content: '' }];
  if (event.event_type === 'TOOL_CALL') return [{ event, kind: 'tool', title: eventName, content: thought || content }];
  if (event.event_type === 'TOOL_RESULT') return [{ event, kind: 'tool', title: eventName, content }];
  if (event.event_type === 'ERROR') return [{ event, kind: 'error', title: '执行遇到问题', content }];
  if (event.event_type === 'COMPLETED') {
    // OpenHands has two formal final-response paths: an assistant MessageEvent
    // and FinishAction.message. A FinishAction may also carry top-level
    // commentary, so expand that one formal event into process + final UI rows.
    if (eventName !== 'FinishAction') return [];
    return [
      ...(thought ? [{ event, kind: 'thought' as const, title: '分析', content: thought }] : []),
      ...(content ? [{ event, kind: 'assistant' as const, title: '', content }] : []),
    ];
  }
  // STATE is transport progress rather than conversation content. Other empty
  // protocol frames are similarly excluded from the product transcript.
  return content ? [{ event, kind: 'thought', title: eventName, content }] : [];
}

function orderedEvents(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  // REST and live frames can arrive in a different order.  Event identity is
  // authoritative: preserve the stable API order between unrelated events,
  // but always render a parent before its descendants.
  const byId = new Map(events.map(event => [event.id, event]));
  const children = new Map<string, OpenHandsConversationEvent[]>();
  const roots: OpenHandsConversationEvent[] = [];
  for (const event of events) {
    const parentId = event.payload.parent_id;
    if (parentId && byId.has(parentId)) {
      const bucket = children.get(parentId) ?? [];
      bucket.push(event);
      children.set(parentId, bucket);
    } else roots.push(event);
  }
  const output: OpenHandsConversationEvent[] = [];
  const seen = new Set<string>();
  const visit = (event: OpenHandsConversationEvent) => {
    if (seen.has(event.id)) return;
    seen.add(event.id);
    output.push(event);
    for (const child of children.get(event.id) ?? []) visit(child);
  };
  for (const event of roots) visit(event);
  for (const event of events) visit(event);
  return output;
}

function isHistoricalAutoTitleError(
  event: OpenHandsConversationEvent,
  events: OpenHandsConversationEvent[],
): boolean {
  if (event.event_type !== 'ERROR' || event.payload.error_code !== 'NotFoundError') return false;
  // OpenHands 1.42 emitted this exact auxiliary title-generation failure as a
  // generic ConversationErrorEvent. It is only safe to suppress when a normal
  // assistant response is already present; other 404s remain visible.
  const detail = String(event.payload.content ?? '');
  const isKnownTitleProtocolFailure = detail.includes('litellm.NotFoundError')
    && detail.includes('OpenAIException')
    && detail.includes('Error code: 404');
  if (isKnownTitleProtocolFailure) {
    return events.some(candidate => {
      const assistantMessage = candidate.event_type === 'MESSAGE'
        && !['user', 'human'].includes(String(candidate.payload.source ?? '').toLowerCase());
      const finishResponse = candidate.event_type === 'COMPLETED'
        && candidate.payload.event_name === 'FinishAction';
      return (assistantMessage || finishResponse) && Boolean(candidate.payload.content);
    });
  }
  const byId = new Map(events.map(candidate => [candidate.id, candidate]));
  const userAncestor = (candidate: OpenHandsConversationEvent): string | undefined => {
    const visited = new Set<string>();
    let current: OpenHandsConversationEvent | undefined = candidate;
    while (current && !visited.has(current.id)) {
      visited.add(current.id);
      const source = String(current.payload.source ?? '').toLowerCase();
      if (current.event_type === 'MESSAGE' && (source === 'user' || source === 'human')) {
        return current.id;
      }
      const parentId: string | undefined = current.payload.parent_id ?? undefined;
      current = parentId ? byId.get(parentId) : undefined;
    }
    return undefined;
  };
  const root = userAncestor(event);
  if (!root) return false;
  return events.some(candidate => {
    if (candidate.event_type !== 'MESSAGE') return false;
    const source = String(candidate.payload.source ?? '').toLowerCase();
    return source !== 'user' && source !== 'human'
      && Boolean(candidate.payload.content)
      && userAncestor(candidate) === root;
  });
}

function turnsFor(events: OpenHandsConversationEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  const ordered = orderedEvents(events);
  for (const event of ordered) {
    if (isHistoricalAutoTitleError(event, ordered)) continue;
    for (const item of itemsFor(event)) {
      if (item.kind === 'user') {
        current = { id: item.event.id, user: item, activity: [] };
        turns.push(current);
        continue;
      }
      if (!current) {
        current = { id: item.event.id, activity: [] };
        turns.push(current);
      }
      // A manual condensation can be requested while the Agent is idle, after
      // the preceding turn already reached a formal assistant/error terminal.
      // Keep that audit record standalone instead of attaching it to the old
      // turn and inventing elapsed work after the terminal. Automatic
      // condensation during a running turn remains in that turn.
      if (item.kind === 'condensation' && (current.assistant || current.activity.some(value => value.kind === 'error'))) {
        current = { id: item.event.id, activity: [item] };
        turns.push(current);
        continue;
      }
      if (item.kind === 'assistant') current.assistant = item;
      else current.activity.push(item);
    }
  }
  return turns;
}

function detailText(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, 500) : '';
}

function detailContent(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, 12_000) : '';
}

function workspacePath(value: string): string {
  return value.replace(/^\/runtime\/workspace\/project\/?/, '工作区/');
}

function compactCommand(value: string): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > 110 ? `${compact.slice(0, 107)}...` : compact;
}

function messageSummary(value: string): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  if (!compact) return '空消息';
  return compact.length > 72 ? `${compact.slice(0, 69)}...` : compact;
}

function groupedActivities(items: Item[]): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  const actionsById = new Map<string, ActivityEntry>();
  const actionsByToolCall = new Map<string, ActivityEntry>();
  for (const item of items) {
    if (item.kind !== 'tool' || item.event.event_type !== 'TOOL_CALL') continue;
    const entry = { id: item.event.id, item, action: item, results: [] } satisfies ActivityEntry;
    actionsById.set(item.event.id, entry);
    const toolCallId = detailText(item.event.payload.tool_call_id);
    if (toolCallId) actionsByToolCall.set(toolCallId, entry);
  }
  const emitted = new Set<ActivityEntry>();
  for (const item of items) {
    if (item.kind === 'tool' && item.event.event_type === 'TOOL_CALL') {
      const entry = actionsById.get(item.event.id)!;
      if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
      continue;
    }
    if (item.kind === 'tool' && item.event.event_type === 'TOOL_RESULT') {
      const actionId = detailText(item.event.payload.action_id);
      const toolCallId = detailText(item.event.payload.tool_call_id);
      const entry = (actionId ? actionsById.get(actionId) : undefined)
        ?? (toolCallId ? actionsByToolCall.get(toolCallId) : undefined);
      if (entry) {
        entry.results.push(item);
        if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
        continue;
      }
    }
    entries.push({ id: item.event.id, item, results: item.event.event_type === 'TOOL_RESULT' ? [item] : [] });
  }
  return entries;
}

interface ActivityPresentation {
  title: string;
  status: string;
  thought?: string;
  command?: string;
  path?: string;
  operation?: string;
  output?: string;
  exitCode?: string;
  actionDetails?: Record<string, unknown>;
  resultDetails?: Record<string, unknown>;
}

function activityPresentation(entry: ActivityEntry, active: boolean): ActivityPresentation {
  const item = entry.action ?? entry.item;
  if (item.kind === 'condensation') {
    return { title: item.title, status: item.event.event_type === 'CONDENSATION_COMPLETED' ? '已完成' : '处理中' };
  }
  if (item.kind === 'thought') {
    return {
      title: active ? '正在分析' : '分析',
      status: active ? '分析中' : '已完成',
      thought: item.content.slice(0, 2_000) || undefined,
    };
  }
  if (item.kind === 'error') return { title: '执行遇到问题', status: '失败' };
  const details = item.event.payload.details ?? {};
  const result = entry.results.at(-1);
  const resultDetails = result?.event.payload.details ?? {};
  const eventName = String(item.event.payload.event_name ?? '');
  const resultName = String(result?.event.payload.event_name ?? '');
  const path = detailText(details.path) || detailText(resultDetails.path) || detailText(details.file_path) || detailText(details.filename);
  const command = detailContent(details.command) || detailContent(resultDetails.command);
  const completed = entry.results.length > 0 || item.event.event_type === 'TOOL_RESULT';
  const failed = Boolean(resultDetails.is_error) || (typeof resultDetails.exit_code === 'number' && resultDetails.exit_code !== 0);
  const thought = entry.action?.content ? entry.action.content.slice(0, 2_000) : undefined;
  const summary = detailText(entry.action?.event.payload.summary);
  const actionTitle = (fallback: string) => summary || fallback;
  const output = entry.results.map(value => detailContent(value.content)).filter(Boolean).join('\n\n').slice(0, 12_000) || undefined;
  const exitCode = typeof resultDetails.exit_code === 'number' ? String(resultDetails.exit_code) : undefined;
  if (eventName === 'TerminalAction' || eventName === 'TerminalObservation' || resultName === 'TerminalObservation') {
    const verb = failed ? '运行失败' : completed ? '已运行' : '正在运行';
    return {
      title: command ? `${verb} ${compactCommand(command)}` : actionTitle(completed ? '命令已执行' : '正在运行命令'),
      status: failed ? '终端 · 失败' : completed ? '终端 · 已完成' : '终端',
      command, thought, output, exitCode, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'FileEditorAction' || eventName === 'FileEditorObservation' || resultName === 'FileEditorObservation') {
    const operation = command.toLowerCase();
    const verb = operation === 'view' ? (failed ? '读取失败' : completed ? '已读取' : '正在读取')
      : ['create', 'write'].includes(operation) ? (failed ? '创建失败' : completed ? '已创建' : '正在创建')
        : operation === 'undo_edit' ? (failed ? '撤销失败' : completed ? '已撤销编辑' : '正在撤销编辑')
          : ['str_replace', 'insert', 'append'].includes(operation) ? (failed ? '编辑失败' : completed ? '已编辑' : '正在编辑')
            : failed ? '文件操作失败' : completed ? '已完成文件操作' : '正在处理文件';
    const displayPath = path ? workspacePath(path) : '';
    return {
      title: displayPath ? `${verb} ${displayPath}` : actionTitle(verb),
      status: failed ? '文件编辑器 · 失败' : completed ? '文件编辑器 · 已完成' : '文件编辑器',
      path: displayPath || undefined, operation: command || undefined, thought, output, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskTrackerAction') {
    return {
      title: completed ? (command === 'plan' ? '任务列表已更新' : '任务列表已读取') : actionTitle(command === 'plan' ? '正在更新任务列表' : '正在查看任务列表'),
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪', thought, output, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskTrackerObservation') {
    return {
      title: command === 'plan' ? '任务列表已更新' : '任务列表已读取',
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪',
    };
  }
  if (eventName === 'InvokeSkillAction') return { title: completed ? `${actionTitle('技能调用')} · 已完成` : actionTitle('正在使用已启用技能'), status: completed ? '技能 · 已完成' : '技能', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'InvokeSkillObservation') return { title: '技能调用已完成', status: completed ? '已完成' : '处理中' };
  if (eventName.includes('Browser')) return { title: completed ? `${actionTitle('浏览器操作')} · 已完成` : actionTitle('正在操作浏览器'), status: completed ? '浏览器 · 已完成' : '浏览器', thought, output, actionDetails: details, resultDetails };
  if (eventName.includes('MCP')) return { title: completed ? `${actionTitle('MCP 工具调用')} · 已完成` : actionTitle('正在调用 MCP 工具'), status: completed ? 'MCP · 已完成' : 'MCP', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'TaskAction') return { title: completed ? `${actionTitle('子任务')} · 已完成` : actionTitle('正在处理子任务'), status: completed ? '子任务 · 已完成' : '子任务', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'TaskObservation') return { title: '子任务已完成', status: completed ? '已完成' : '处理中' };
  const toolName = eventName.replace(/(?:Action|Observation)$/, '') || '工具';
  return {
    title: completed ? `${actionTitle(toolName)} · 已完成` : actionTitle(`正在使用 ${toolName}`),
    status: completed ? '工具 · 已完成' : '工具',
    thought, output, actionDetails: details, resultDetails,
  };
}

function displayDetails(details: Record<string, unknown>): string {
  const visible = Object.fromEntries(Object.entries(details).filter(([key]) => !['content', 'old_content', 'new_content'].includes(key)));
  return Object.keys(visible).length ? JSON.stringify(visible, null, 2).slice(0, 12_000) : '';
}

function ToolDetailPanel({ presentation, eventName }: { presentation: ActivityPresentation; eventName: string }) {
  const details = presentation.actionDetails ?? {};
  const resultDetails = presentation.resultDetails ?? {};
  const isTerminal = eventName.includes('Terminal');
  const isFile = eventName.includes('FileEditor');
  const structured = displayDetails(details);
  const structuredResult = displayDetails(resultDetails);
  const fileText = detailContent(details.file_text);
  const oldText = detailContent(details.old_str);
  const newText = detailContent(details.new_str);
  const hasDetail = Boolean(presentation.command || presentation.output || structured || structuredResult || fileText || oldText || newText);
  if (!hasDetail) return null;
  return <div className="conversation-tool-detail-panel">
      <b>{isTerminal ? 'Shell' : isFile ? '文件操作' : '工具调用'}</b>
      {isTerminal && presentation.command && <pre><code>{`$ ${presentation.command}`}</code></pre>}
      {isFile && <dl>
        {presentation.operation && <><dt>操作</dt><dd>{presentation.operation}</dd></>}
        {presentation.path && <><dt>路径</dt><dd>{presentation.path}</dd></>}
        {Array.isArray(details.view_range) && <><dt>行范围</dt><dd>{details.view_range.join(' - ')}</dd></>}
        {typeof details.insert_line === 'number' && <><dt>插入行</dt><dd>{details.insert_line}</dd></>}
      </dl>}
      {fileText && <><small>写入内容</small><pre><code>{fileText}</code></pre></>}
      {oldText && <><small>替换前</small><pre><code>{oldText}</code></pre></>}
      {newText && <><small>替换后</small><pre><code>{newText}</code></pre></>}
      {!isTerminal && !isFile && structured && <><small>原始操作</small><pre><code>{structured}</code></pre></>}
      {presentation.output && <><small>执行结果</small><pre><code>{presentation.output}</code></pre></>}
      {!isTerminal && !isFile && structuredResult && <><small>结果信息</small><pre><code>{structuredResult}</code></pre></>}
      {presentation.exitCode && <small>退出码 {presentation.exitCode}</small>}
    </div>;
}

function eventTime(item?: Item): number | undefined {
  return parsedEventTime(item?.event.payload.timestamp);
}

function parsedEventTime(raw: unknown): number | undefined {
  if (typeof raw !== 'string' || !raw) return undefined;
  // OpenHands 1.42.0 creates Event.timestamp with datetime.now().isoformat().
  // The Runtime container runs in UTC, but that value has no timezone suffix.
  // Browsers otherwise interpret it as local time and inflate an active turn by
  // the local UTC offset. Preserve explicitly zoned timestamps as-is.
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  return Number.isFinite(value) ? value : undefined;
}

function condensationTriggeredAt(item: Item): number | undefined {
  return parsedEventTime(item.event.payload.condensation_triggered_at) ?? eventTime(item);
}

function condensationCompletedAt(item: Item): number | undefined {
  return parsedEventTime(item.event.payload.condensation_completed_at) ?? eventTime(item);
}

function turnProcessBlocks(
  items: Item[],
  startedAt: number | undefined,
  finishedAt: number | undefined,
  active: boolean,
): TurnProcessBlock[] {
  const hasCondensation = items.some(item => item.kind === 'condensation');
  if (!hasCondensation) {
    return [{ kind: 'activity', id: 'activity-0', items, startedAt, finishedAt, active }];
  }
  if (items.every(item => item.kind === 'condensation')) {
    return [{ kind: 'condensation', id: 'condensation-0', items }];
  }

  const blocks: TurnProcessBlock[] = [];
  let activityItems: Item[] = [];
  let condensationItems: Item[] = [];
  let segmentStartedAt = startedAt;
  let compressionPending = false;
  let sequence = 0;
  const flushActivity = (segmentFinishedAt: number | undefined, force = false) => {
    if (!activityItems.length && !force) return;
    blocks.push({
      kind: 'activity',
      id: `activity-${sequence++}`,
      items: activityItems,
      startedAt: segmentStartedAt,
      finishedAt: segmentFinishedAt,
      active: false,
    });
    activityItems = [];
  };
  const flushCondensation = () => {
    if (!condensationItems.length) return;
    blocks.push({
      kind: 'condensation',
      id: `condensation-${sequence++}`,
      items: condensationItems,
    });
    condensationItems = [];
  };

  for (const item of items) {
    if (item.kind !== 'condensation') {
      activityItems.push(item);
      continue;
    }
    if (item.event.event_type === 'CONDENSATION_REQUESTED') {
      flushActivity(condensationTriggeredAt(item), true);
      condensationItems.push(item);
      compressionPending = true;
      segmentStartedAt = undefined;
      continue;
    }

    if (!compressionPending) {
      flushActivity(condensationTriggeredAt(item), true);
    }
    condensationItems.push(item);
    flushCondensation();
    compressionPending = false;
    segmentStartedAt = condensationCompletedAt(item);
  }

  if (compressionPending) {
    flushCondensation();
  } else {
    flushActivity(finishedAt, true);
    const latestActivity = [...blocks].reverse().find(
      (block): block is Extract<TurnProcessBlock, { kind: 'activity' }> => block.kind === 'activity',
    );
    if (latestActivity) latestActivity.active = active;
  }
  return blocks;
}

function formatEventTime(raw: unknown): string {
  if (typeof raw !== 'string' || !raw) return '时间未知';
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : raw + 'Z';
  const value = Date.parse(normalized);
  if (!Number.isFinite(value)) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(value);
}

function condensationReason(item: Item): string {
  const detail = item.event.payload.condensation_reason_detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (item.event.event_type === 'CONDENSATION_REQUESTED') {
    return 'OpenHands 收到显式压缩请求，正在整理较早的上下文。';
  }
  return 'OpenHands 自动上下文保护已触发；原生事件未保存更细的触发原因。';
}

function CondensationNotices({ items }: { items: Item[] }) {
  if (!items.length) return null;
  const requestIds = new Set(items
    .filter(item => item.event.event_type === 'CONDENSATION_REQUESTED')
    .map(item => item.event.id));
  return <div className="conversation-condensation-timeline" aria-label="上下文压缩记录">
    {items.flatMap(item => {
      if (item.event.event_type === 'CONDENSATION_REQUESTED') {
        return [<article className="conversation-condensation-notice triggered" key={item.event.id} role="status">
          <CircleAlert size={17}/><div><header><b>已触发上下文压缩</b><time>{formatEventTime(item.event.payload.timestamp)}</time></header><p>{condensationReason(item)}</p></div>
        </article>];
      }
      const requestId = typeof item.event.payload.condensation_request_event_id === 'string'
        ? item.event.payload.condensation_request_event_id
        : undefined;
      const needsRecoveredStart = !requestId || !requestIds.has(requestId);
      const forgotten = Array.isArray(item.event.payload.forgotten_event_ids)
        ? item.event.payload.forgotten_event_ids.length
        : undefined;
      const completedText = forgotten
        ? '已完成摘要并从模型上下文中移除 ' + forgotten + ' 个较早事件；完整事件记录仍然保留。'
        : '已完成较早上下文的摘要；完整事件记录仍然保留。';
      return [
        ...(needsRecoveredStart ? [<article className="conversation-condensation-notice triggered" key={item.event.id + '-triggered'} role="status">
          <CircleAlert size={17}/><div><header><b>已触发上下文压缩</b><time>{formatEventTime(item.event.payload.condensation_triggered_at ?? item.event.payload.timestamp)}</time></header><p>{condensationReason(item)}</p></div>
        </article>] : []),
        <article className="conversation-condensation-notice completed" key={item.event.id} role="status">
          <Check size={17}/><div><header><b>上下文压缩已完成</b><time>{formatEventTime(item.event.payload.condensation_completed_at ?? item.event.payload.timestamp)}</time></header><p>{completedText}</p></div>
        </article>,
      ];
    })}
  </div>;
}

function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}小时${minutes ? `${minutes}分钟` : ''}${remainder ? `${remainder}秒` : ''}`;
  if (minutes) return `${minutes}分钟${remainder ? `${remainder}秒` : ''}`;
  return `${remainder}秒`;
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Some embedded or permission-restricted browsers still allow the
      // user-gesture fallback below.
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('Clipboard is unavailable');
}

function elementForNode(node: Node | null): Element | null {
  if (node instanceof Element) return node;
  return node?.parentElement ?? null;
}

function isolatedUserSelection(selection: Selection): { content: HTMLElement; text: string } | undefined {
  if (!selection.rangeCount) return undefined;
  const anchor = elementForNode(selection.anchorNode);
  const focus = elementForNode(selection.focusNode);
  const content = anchor?.closest<HTMLElement>('.conversation-message.user .conversation-message-content');
  if (!content || !focus) return undefined;
  const range = selection.getRangeAt(0);
  if (content.contains(range.startContainer) && content.contains(range.endContainer)) {
    return { content, text: selection.toString() };
  }
  // A browser selection that starts in a user bubble may accidentally continue
  // into the following turn as the surface updates. Keep the copy operation
  // faithful to the message the user started selecting, not its descendants.
  return { content, text: content.innerText };
}

function useElapsedSeconds(startedAt: number | undefined, finishedAt: number | undefined, active: boolean): number | undefined {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startedAt === undefined) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);
  if (startedAt === undefined) return undefined;
  return Math.max(0, ((finishedAt ?? now) - startedAt) / 1000);
}

function activeToolLabel(eventName: string): string {
  if (eventName.includes('Terminal')) return '正在后台执行命令';
  if (eventName.includes('FileEditor')) return '正在处理文件';
  if (eventName.includes('Browser')) return '正在执行浏览器操作';
  if (eventName.includes('MCP')) return '正在调用 MCP 工具';
  if (eventName.includes('Skill')) return '正在使用技能';
  if (eventName.includes('Task')) return '正在处理任务';
  return '正在执行工具';
}

function activeActivityLabel(entries: ActivityEntry[], requestSubmitting: boolean): string {
  if (requestSubmitting) return '正在提交消息';
  const pendingTool = [...entries].reverse().find(entry => entry.action?.kind === 'tool' && entry.results.length === 0)?.action;
  if (pendingTool) return activeToolLabel(String(pendingTool.event.payload.event_name ?? ''));
  const latest = entries.at(-1)?.item;
  if (latest?.kind === 'condensation' && latest.event.event_type === 'CONDENSATION_REQUESTED') return '正在压缩上下文';
  return '正在思考';
}

function CurrentTurnStatus({ items, liveText, requestSubmitting }: {
  items: Item[];
  liveText: string;
  requestSubmitting: boolean;
}) {
  const label = liveText
    ? '正在生成回复'
    : activeActivityLabel(groupedActivities(items), requestSubmitting);
  return <div className="conversation-turn-status" role="status" aria-label={label}>
    <span>{label}</span>
    <span className="conversation-turn-status-dots" aria-hidden="true"><i/><i/><i/></span>
  </div>;
}

function ActivityGroup({ items, active, liveText, startedAt, finishedAt }: {
  items: Item[];
  active: boolean;
  liveText?: string;
  startedAt?: number;
  finishedAt?: number;
}) {
  const elapsedSeconds = useElapsedSeconds(startedAt, finishedAt, active);
  const entries = groupedActivities(items);
  const itemCount = entries.length + (liveText ? 1 : 0);
  const [open, setOpen] = useState(active);
  useEffect(() => { setOpen(active); }, [active]);
  const label = active
    ? elapsedSeconds === undefined ? '处理中' : `已耗时 ${formatDuration(elapsedSeconds)}`
    : finishedAt === undefined || elapsedSeconds === undefined ? '工作过程' : `耗时 ${formatDuration(elapsedSeconds)}`;
  const summary = <><ChevronRight size={14}/><span>{label}</span>{itemCount > 0 && <small>{itemCount} 项</small>}{active && <LoaderCircle className="conversation-activity-spin" size={13}/>}</>;
  const hasDetails = itemCount > 0;
  if (!hasDetails) return <div className="conversation-activity-group summary-only"><div className="conversation-activity-summary">{summary}</div></div>;
  return <details className={`conversation-activity-group${active ? ' active' : ''}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{summary}</summary>
    <div className="conversation-activity-list">
      {entries.map(entry => {
        const item = entry.action ?? entry.item;
        const Icon = item.kind === 'error' ? CircleAlert : item.kind === 'thought' || item.kind === 'condensation' ? Sparkles : Wrench;
        const eventName = String(item.event.payload.event_name ?? '');
        const ToolIcon = eventName.includes('Terminal') ? SquareTerminal : eventName.includes('FileEditor') ? FileText : Icon;
        const presentation = activityPresentation(entry, active);
        const toolDetail = item.kind === 'tool' ? <ToolDetailPanel presentation={presentation} eventName={eventName}/> : null;
        if (item.kind === 'tool' && toolDetail) return <div className="conversation-tool-entry" key={entry.id}>
          {presentation.thought && <article className="conversation-activity-row thought"><Sparkles size={14}/><div><MessageMarkdown>{presentation.thought}</MessageMarkdown></div></article>}
          <details className="conversation-activity-row tool conversation-tool-detail">
            <summary><ToolIcon size={14}/><div><b>{presentation.title}</b><small>{presentation.status}</small></div><ChevronRight className="conversation-tool-chevron" size={13}/></summary>
            {toolDetail}
          </details>
        </div>;
        return <article className={`conversation-activity-row ${item.kind}`} key={entry.id}>
          <ToolIcon size={14}/><div><b>{presentation.title}</b><small>{presentation.status}</small>
            {presentation.thought && <MessageMarkdown>{presentation.thought}</MessageMarkdown>}
          </div>
        </article>;
      })}
      {liveText && <article className="conversation-activity-row live-text"><Sparkles size={14}/><div><b>正在生成回复</b><small>模型输出</small><p className="conversation-live-text-content">{liveText}</p></div></article>}
    </div>
  </details>;
}

function AgentReply({ event, content, onFork }: { event: OpenHandsConversationEvent; content: string; onFork?: () => void }) {
  const eventId = event.id;
  const outputs = event.event_type === 'COMPLETED' && event.payload.event_name === 'FinishAction'
    ? candidateOutputs(content)
    : undefined;
  return <article className="conversation-message assistant" data-turn-terminal="true" data-event-id={eventId}>
    {outputs ? <CandidateOutputReply outputs={outputs}/> : content ? <MessageMarkdown>{content}</MessageMarkdown> : <span className="conversation-typing"><i/><i/><i/></span>}
    {onFork && <button type="button" className="conversation-message-fork" onClick={onFork}><GitFork size={12}/>从此处分叉会话</button>}
  </article>;
}

const NETWORK_ERROR_CODES = new Set([
  'LLMServiceUnavailableError',
  'LLMTimeoutError',
  'LLMNoResponseError',
  'APIConnectionError',
  'ReadTimeout',
  'RequestError',
]);

function ConversationFailure({ item }: { item: Item }) {
  const code = typeof item.event.payload.error_code === 'string' ? item.event.payload.error_code : '';
  // OpenHands 1.42 emitted failed auto-title metadata as a regular terminal
  // ConversationErrorEvent. The runtime patch prevents new events; this is a
  // final rendering safeguard for already-persisted history regardless of the
  // event order or branch projection returned by an older runtime.
  const isLegacyAutoTitleFailure = code === 'NotFoundError'
    && item.content.includes('litellm.NotFoundError')
    && item.content.includes('OpenAIException')
    && item.content.includes('Error code: 404');
  if (isLegacyAutoTitleFailure) return null;
  const content = code === 'LLMRateLimitError'
    ? '模型服务拒绝了这次请求：当前配置的账户可用额度已用尽。请选择有可用额度的模型配置后，编辑并重新思考此消息。'
    : NETWORK_ERROR_CODES.has(code)
      ? '网络连接异常，模型服务在 5 次尝试后仍未响应。本轮已停止，请检查网络或模型服务后重试。'
    : item.content || 'OpenHands 未能完成这一轮，请检查模型配置后重试。';
  return <article className="conversation-failure" data-turn-terminal="true" data-event-id={item.event.id} role="status">
    <CircleAlert size={15}/><div><b>本轮没有生成回复</b><p>{content}</p>{code && <small>{code}</small>}</div>
  </article>;
}

export function ConversationSurface({ events, liveText, isGenerating, requestStartedAt, requestSubmitting = false, rewritePending = false, condensationStatus, onRetryCondensation, onRewrite, onFork, onOpenAttachment }: {
  events: OpenHandsConversationEvent[];
  liveText: string;
  isGenerating: boolean;
  requestStartedAt?: number;
  requestSubmitting?: boolean;
  rewritePending?: boolean;
  condensationStatus?: { state: 'running' | 'failed'; startedAt: number; message?: string };
  onRetryCondensation?: () => void;
  onRewrite?: (eventId: string, content: string) => void;
  onFork?: (eventId: string) => void;
  onOpenAttachment?: (attachment: AgentAttachment) => void;
}) {
  const surface = useRef<HTMLElement>(null);
  const shell = useRef<HTMLDivElement>(null);
  const initialPositioned = useRef(false);
  const followLatest = useRef(true);
  const wasGenerating = useRef(isGenerating);
  const copyResetTimer = useRef<number | undefined>(undefined);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const [copiedEventId, setCopiedEventId] = useState<string>();
  const [messagePreview, setMessagePreview] = useState<{ id: string; content: string; index: number; top: number }>();
  const [condensationElapsed, setCondensationElapsed] = useState(0);
  const turns = useMemo(() => turnsFor(events), [events]);
  const userMessageNavigation = useMemo<UserMessageNavigationItem[]>(() => turns.flatMap(turn => turn.user ? [{
    id: turn.user.event.id,
    content: turn.user.content,
  }] : []), [turns]);
  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    followLatest.current = true;
    setIsAtLatest(true);
    const element = surface.current;
    element?.scrollTo({ top: element.scrollHeight, behavior });
  }, []);
  const updateScrollPosition = useCallback(() => {
    const element = surface.current;
    if (!element) return;
    const atLatest = element.scrollHeight - element.scrollTop - element.clientHeight <= 16;
    followLatest.current = atLatest;
    setIsAtLatest(atLatest);
  }, []);
  const scrollToUserMessage = useCallback((eventId: string) => {
    const element = surface.current;
    const target = element?.querySelectorAll<HTMLElement>('[data-user-event-id]');
    const message = Array.from(target ?? []).find(item => item.dataset.userEventId === eventId);
    if (!element || !message) return;
    const top = message.getBoundingClientRect().top - element.getBoundingClientRect().top + element.scrollTop - 18;
    followLatest.current = false;
    element.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }, []);
  const showMessagePreview = useCallback((message: UserMessageNavigationItem, index: number, target: HTMLElement) => {
    const shellBounds = shell.current?.getBoundingClientRect();
    const targetBounds = target.getBoundingClientRect();
    if (!shellBounds) return;
    setMessagePreview({
      id: message.id,
      content: message.content,
      index,
      top: targetBounds.top - shellBounds.top + targetBounds.height / 2,
    });
  }, []);
  const handleScroll = useCallback(() => {
    updateScrollPosition();
  }, [updateScrollPosition]);
  const scrollToTerminalStart = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const terminals = surface.current?.querySelectorAll<HTMLElement>('[data-turn-terminal="true"]');
    const terminal = terminals?.[terminals.length - 1];
    if (!terminal) return scrollToLatest(behavior);
    terminal.scrollIntoView({ block: 'start', behavior });
    window.requestAnimationFrame(updateScrollPosition);
  }, [scrollToLatest, updateScrollPosition]);
  const currentHasTerminal = isGenerating && Boolean(turns.at(-1)?.assistant || turns.at(-1)?.activity.some(item => item.kind === 'error'));
  useLayoutEffect(() => {
    if (!initialPositioned.current && (turns.length || liveText || isGenerating)) {
      initialPositioned.current = true;
      scrollToLatest('auto');
    } else if (!wasGenerating.current && isGenerating) {
      scrollToLatest('smooth');
    } else if (wasGenerating.current && !isGenerating && followLatest.current) {
      scrollToTerminalStart('auto');
    } else if (followLatest.current && !currentHasTerminal) {
      scrollToLatest('auto');
    }
    wasGenerating.current = isGenerating;
  }, [currentHasTerminal, isGenerating, liveText, scrollToLatest, scrollToTerminalStart, turns.length]);
  useEffect(() => () => {
    if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
  }, []);
  useEffect(() => {
    if (condensationStatus?.state !== 'running') return;
    const update = () => setCondensationElapsed(Math.max(0, Date.now() - condensationStatus.startedAt));
    update();
    const timer = window.setInterval(update, 1_000);
    if (followLatest.current) window.requestAnimationFrame(() => scrollToLatest('smooth'));
    return () => window.clearInterval(timer);
  }, [condensationStatus, scrollToLatest]);
  useEffect(() => {
    const onCopy = (event: ClipboardEvent) => {
      const selection = window.getSelection();
      if (!selection || !surface.current) return;
      const isolated = isolatedUserSelection(selection);
      if (!isolated || !surface.current.contains(isolated.content)) return;
      event.preventDefault();
      event.clipboardData?.setData('text/plain', isolated.text);
    };
    document.addEventListener('copy', onCopy);
    return () => document.removeEventListener('copy', onCopy);
  }, []);
  const copyUserMessage = useCallback((eventId: string, content: string) => {
    void copyText(content).then(() => {
      setCopiedEventId(eventId);
      if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
      copyResetTimer.current = window.setTimeout(() => setCopiedEventId(current => current === eventId ? undefined : current), 1_500);
    }).catch(() => {
      // Native selection copy remains available when the browser rejects programmatic clipboard access.
    });
  }, []);
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !liveText && !isGenerating && !condensationStatus) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  const showJumpToLatest = !isAtLatest && Boolean(turns.length || liveText || isGenerating);
  return <div ref={shell} className="conversation-surface-shell">
    {userMessageNavigation.length > 0 && <nav className="conversation-message-index" aria-label="用户消息导航">
      {userMessageNavigation.map((message, index) => <button
        type="button"
        key={message.id}
        aria-label={`定位到用户消息：${messageSummary(message.content)}`}
        aria-describedby={messagePreview?.id === message.id ? 'conversation-message-preview' : undefined}
        onPointerEnter={event => showMessagePreview(message, index, event.currentTarget)}
        onFocus={event => showMessagePreview(message, index, event.currentTarget)}
        onPointerLeave={() => setMessagePreview(current => current?.id === message.id ? undefined : current)}
        onBlur={() => setMessagePreview(current => current?.id === message.id ? undefined : current)}
        onClick={() => scrollToUserMessage(message.id)}
      >
        <span className="conversation-message-index-tick" aria-hidden="true"/>
      </button>)}
    </nav>}
    {messagePreview && <aside id="conversation-message-preview" className="conversation-message-index-tooltip" role="tooltip" style={{ top: messagePreview.top }}><span>{messagePreview.content || '（空消息）'}</span></aside>}
    <section ref={surface} className="conversation-surface" aria-live="polite" onScroll={handleScroll}>
      {turns.map((turn, index) => {
        const isCurrent = index === turns.length - 1 && isGenerating;
        const failures = turn.activity.filter(item => item.kind === 'error');
        const startedAt = eventTime(turn.user) ?? (isCurrent ? requestStartedAt : undefined);
        const finishedAt = eventTime(turn.assistant ?? failures.at(-1));
        const processBlocks = turnProcessBlocks(
          turn.activity.filter(item => item.kind !== 'error'),
          startedAt,
          finishedAt,
          isCurrent && !turn.assistant && !failures.length,
        );
        return <section className="conversation-turn" key={turn.id}>
          {turn.user && (editingEventId === turn.user.event.id ? <form data-user-event-id={turn.user.event.id} className="conversation-message user conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form> : <article data-user-event-id={turn.user.event.id} className="conversation-message user">{turn.user.content && <div className="conversation-message-content"><MessageMarkdown>{turn.user.content}</MessageMarkdown></div>}<MessageAttachments attachments={eventAttachments(turn.user.event)} onOpen={onOpenAttachment}/><div className="conversation-message-actions"><button type="button" className="conversation-message-copy" aria-label={copiedEventId === turn.user.event.id ? '消息已复制' : '复制消息'} title={copiedEventId === turn.user.event.id ? '已复制' : '复制消息'} onClick={() => copyUserMessage(turn.user!.event.id, turn.user!.content)}>{copiedEventId === turn.user.event.id ? <Check size={13}/> : <Copy size={13}/>}</button>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" aria-label="编辑并重新思考" title="编辑并重新思考" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}><Pencil size={13}/></button>}</div></article>)}
          {processBlocks.map((block, blockIndex) => block.kind === 'condensation'
            ? <CondensationNotices key={block.id} items={block.items}/>
            : <ActivityGroup
              key={block.id}
              items={block.items}
              active={block.active}
              liveText={isCurrent && blockIndex === processBlocks.length - 1 ? liveText : undefined}
              startedAt={block.startedAt}
              finishedAt={block.finishedAt}
            />)}
          {isCurrent && !turn.assistant && !failures.length && <CurrentTurnStatus items={turn.activity} liveText={liveText} requestSubmitting={requestSubmitting}/>}
          {turn.assistant && <AgentReply event={turn.assistant.event} content={turn.assistant.content} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined}/>}
          {failures.map(item => <ConversationFailure key={item.event.id} item={item}/>)}
        </section>;
      })}
      {turns.length === 0 && (liveText || isGenerating) && <><ActivityGroup items={[]} active liveText={liveText} startedAt={requestStartedAt}/><CurrentTurnStatus items={[]} liveText={liveText} requestSubmitting={requestSubmitting}/></>}
      {condensationStatus && <article className={`conversation-condensation-progress ${condensationStatus.state}`} aria-label={condensationStatus.state === 'running' ? '正在压缩上下文' : '上下文压缩失败'} role="status">
        {condensationStatus.state === 'running' ? <LoaderCircle className="conversation-condensation-spinner" size={16}/> : <CircleAlert size={16}/>}
        <div><header><b>{condensationStatus.state === 'running' ? '正在压缩上下文' : '上下文压缩未完成'}</b>{condensationStatus.state === 'running' && <time>{formatDuration(condensationElapsed / 1_000)}</time>}</header>
          <p>{condensationStatus.state === 'failed'
            ? condensationStatus.message || 'OpenHands 未能完成上下文压缩，请稍后重试。'
            : condensationElapsed < 2_000
              ? '已提交原生压缩请求，正在等待 OpenHands 接收。'
              : condensationElapsed < 20_000
                ? 'Condenser 正在生成较早上下文的结构化摘要。'
                : '正在等待摘要完成，并校验用户目标、已完成事项与待办。'}</p>
          {condensationStatus.state === 'failed' && onRetryCondensation && <button type="button" onClick={onRetryCondensation}>重新压缩</button>}
        </div>
      </article>}
    </section>
    {showJumpToLatest && <button
      type="button"
      className={`conversation-jump-latest${isGenerating ? ' generating' : ''}`}
      aria-label={isGenerating ? '跳转到正在生成的最新回复' : '跳转到最新回复'}
      title={isGenerating ? '查看正在生成的最新回复' : '查看最新回复'}
      onClick={() => scrollToLatest()}
    >
      {isGenerating ? (
        <span className="conversation-jump-dots" aria-hidden="true"><i/><i/><i/></span>
      ) : (
        <ChevronDown size={19}/>
      )}
    </button>}
  </div>;
}
