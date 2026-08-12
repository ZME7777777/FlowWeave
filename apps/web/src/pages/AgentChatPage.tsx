import { AlertTriangle, ArrowLeft, Bot, Check, CheckCircle2, ChevronDown, ChevronRight, Clock3, CornerDownRight, Download, File as FileIcon, FileText, GitFork, Image as ImageIcon, Link2, LoaderCircle, Paperclip, Pencil, Plus, RefreshCw, Send, Sparkles, Square, Trash2, Workflow, X, XCircle } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyboardEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import '../agent-chat.css';
import { api, messageAttachmentUrl, randomId, subscribeToConversationStream, subscribeToRun, workspaceImageUrl } from '../api/client';
import { AgentRuntimeSidebar } from '../components/AgentRuntimeSidebar';
import { useProductDialog } from '../components/ProductDialogContext';
import { useWorkbenchStore } from '../store/workbench';
import type { AgentConversation, AgentMessage, AgentMessageAttachmentPart, MessageAttachmentInput, NodeAttempt, ProviderModel } from '../types';

const MAX_ATTACHMENT_COUNT = 4;
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024;
interface PendingAttachment extends MessageAttachmentInput { id: string; preview_url?: string }
interface AttachmentPreview { messageId: string; item: AgentMessageAttachmentPart }
const STATE_LABELS: Record<string, string> = {
  CREATING: '创建中', IDLE: '在线', GENERATING: 'Agent 生成中',
  STOPPING: '正在停止',
  WAITING_HUMAN: '等待你的回复', WAITING_SUBAGENTS: '等待子智能体', FAILED: '连接失败', READ_ONLY: '历史会话',
};
const STARTED_STATES = new Set(['WAITING_START_CONFIRMATION', 'EXECUTING', 'WAITING_HUMAN', 'END_GATES', 'END_BLOCKED', 'WAITING_ACCEPTANCE']);
const conversationTitle = (title: string) => title.replace(/Attempt\s*(\d+)/gi, '第 $1 轮');
const capabilityMarker = (_type: string, key: string) => `$${key}`;
const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** FlowWeave human-control mark: a person inside an open control circuit. */
function HumanControlIcon({ size = 16 }: { size?: number }) {
  return <svg className="human-control-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M5.5 18.5v-1.25A4.75 4.75 0 0 1 10.25 12.5h1.5a4.75 4.75 0 0 1 4.75 4.75v1.25" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
    <circle cx="11" cy="7.5" r="3" stroke="currentColor" strokeWidth="1.8"/>
    <path d="M18.5 6.25v4.5M16.25 8.5h4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
    <circle cx="18.5" cy="8.5" r="3.5" stroke="currentColor" strokeWidth="1.2" strokeDasharray="1.5 1.5"/>
  </svg>;
}

function SourceIcon({ source, size = 16 }: { source: AgentMessage['source']; size?: number }) {
  if (source === 'HUMAN') return <HumanControlIcon size={size}/>;
  if (source === 'PROGRAM') return <Workflow size={size}/>;
  return <Bot size={size}/>;
}

function messageText(message: AgentMessage): string {
  if (message.content.parts?.length) return message.content.parts.filter(part => part.type === 'text').map(part => part.text).join('\n');
  if (message.content.tool) return JSON.stringify(message.content.tool, null, 2);
  if (message.content.error) {
    const error = eventDetails(message.content.error);
    return typeof error.message === 'string' ? error.message : JSON.stringify(error, null, 2);
  }
  return JSON.stringify(message.content, null, 2);
}

const messageAttachments = (message: AgentMessage) => (message.content.parts ?? []).filter((part): part is AgentMessageAttachmentPart => part.type === 'attachment');
const messageSummary = (message: AgentMessage) => messageText(message) || messageAttachments(message).map(item => item.filename).join('、') || '附件消息';
const formatBytes = (value: number) => value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;

async function prepareBrowserAttachment(file: File): Promise<PendingAttachment> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('读取附件失败'));
    reader.readAsDataURL(file);
  });
  return {
    id: randomId(),
    filename: file.name,
    mime_type: file.type || 'application/octet-stream',
    byte_size: file.size,
    content_base64: dataUrl.slice(dataUrl.indexOf(',') + 1),
    preview_url: file.type.startsWith('image/') ? dataUrl : undefined,
  };
}

const isWorkspaceImage = (value: string) => value.startsWith('file:///workspaces/') || value.startsWith('/workspaces/') || value.startsWith('/data/workspaces/') || value.startsWith('./') || value.startsWith('../');

function WorkspaceMarkdownImage({ messageId, source, alt }: { messageId: string; source: string; alt: string }) {
  const [retry, setRetry] = useState(0);
  const [failed, setFailed] = useState(false);
  const maxAutomaticRetries = 6;

  useEffect(() => {
    setRetry(0);
    setFailed(false);
  }, [messageId, source]);

  useEffect(() => {
    if (!failed || retry >= maxAutomaticRetries) return;
    const timer = window.setTimeout(() => {
      setRetry(value => value + 1);
      setFailed(false);
    }, Math.min(1000 * (2 ** retry), 30_000));
    return () => window.clearTimeout(timer);
  }, [failed, retry]);

  if (failed) {
    const exhausted = retry >= maxAutomaticRetries;
    return <span className="workspace-image-retry" role="status">
      <ImageIcon size={18}/>
      <span>{exhausted ? `${alt || '图片'}加载失败` : `${alt || '图片'}加载失败，正在重试…`}</span>
      {exhausted && <button type="button" onClick={() => { setRetry(value => value + 1); setFailed(false); }}>重新加载</button>}
    </span>;
  }

  const url = `${workspaceImageUrl(messageId, source)}&retry=${retry}`;
  return <img key={url} src={url} alt={alt} onError={() => setFailed(true)}/>;
}

function MarkdownMessage({ text, messageId }: { text: string; messageId?: string }) {
  return <div className="message-markdown"><ReactMarkdown
    urlTransform={value => isWorkspaceImage(value) ? value : defaultUrlTransform(value)}
    components={{ img: ({ src, alt }) => messageId && src && isWorkspaceImage(src)
      ? <WorkspaceMarkdownImage messageId={messageId} source={src} alt={alt ?? ''}/>
      : <img src={src} alt={alt ?? ''}/> }}
  >{text}</ReactMarkdown></div>;
}

function AttachmentPreviewDialog({ preview, onClose }: { preview: AttachmentPreview; onClose: () => void }) {
  const { messageId, item } = preview;
  const inlineUrl = messageAttachmentUrl(messageId, item.attachment_id);
  const downloadUrl = messageAttachmentUrl(messageId, item.attachment_id, true);
  const canEmbed = item.mime_type.startsWith('image/') || item.mime_type === 'application/pdf' || item.mime_type.startsWith('text/');
  useEffect(() => {
    const close = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [onClose]);
  return <div className="attachment-preview-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="attachment-preview-dialog" role="dialog" aria-modal="true" aria-label={`预览 ${item.filename}`}>
      <header><div><b>{item.filename}</b><small>{item.mime_type} · {formatBytes(item.byte_size)}</small></div><div><a className="secondary" href={downloadUrl}><Download size={13}/>下载</a><button type="button" className="ghost" aria-label="关闭预览" onClick={onClose}><X size={17}/></button></div></header>
      <div className={`attachment-preview-content ${item.mime_type.startsWith('image/') ? 'image' : ''}`}>{item.mime_type.startsWith('image/')
        ? <img src={inlineUrl} alt={item.filename}/>
        : canEmbed ? <iframe src={inlineUrl} title={item.filename}/>
          : <div className="attachment-preview-unsupported"><FileIcon size={34}/><b>此格式暂不支持站内预览</b><span>可下载后使用本地应用打开。</span><a className="primary" href={downloadUrl}><Download size={14}/>下载文件</a></div>}
      </div>
    </section>
  </div>;
}

function MessageAttachmentList({ message, onPreview }: { message: AgentMessage; onPreview: (preview: AttachmentPreview) => void }) {
  const attachments = messageAttachments(message);
  if (!attachments.length) return null;
  return <div className="message-attachments">{attachments.map(item => {
    const url = messageAttachmentUrl(message.id, item.attachment_id);
    return <article key={item.attachment_id} className={item.mime_type.startsWith('image/') ? 'image' : ''}>
      <button type="button" className="attachment-preview-trigger" title={`预览 ${item.filename}`} onClick={() => onPreview({ messageId: message.id, item })}>
        {item.mime_type.startsWith('image/') ? <img src={url} alt={item.filename}/> : <span><FileIcon size={16}/></span>}
        <span><b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small></span>
      </button>
      <a href={messageAttachmentUrl(message.id, item.attachment_id, true)} title={`下载 ${item.filename}`} aria-label={`下载 ${item.filename}`}><Download size={13}/></a>
    </article>;
  })}</div>;
}

function eventDetails(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function usefulDetails(value: unknown): Record<string, unknown> {
  return Object.fromEntries(Object.entries(eventDetails(value)).filter(([, item]) => item !== '' && item !== null && item !== undefined));
}

type ToolActivityKind = 'read' | 'create' | 'edit' | 'undo-edit' | 'patch' | 'command' | 'search' | 'web' | 'delegate' | 'task-view' | 'task-update' | 'workflow' | 'consult' | 'thought' | 'generic';
type ToolActivityStatus = 'running' | 'completed' | 'failed' | 'ended';

function toolEventType(tool: Record<string, unknown>) {
  return String(tool.event_name ?? '')
    .replace(/(Action|Observation|Event)$/i, '')
    .toLowerCase();
}

function toolActivityKind(tool: Record<string, unknown>): ToolActivityKind {
  const details = eventDetails(tool.details);
  const eventType = toolEventType(tool);
  const command = typeof details.command === 'string' ? details.command.toLowerCase() : '';

  // A command only has meaning within its tool. For example, `view` means
  // reading a file in FileEditor, but viewing the task list in TaskTracker.
  if (eventType.includes('fileeditor')) {
    if (command === 'view') return 'read';
    if (command === 'create') return 'create';
    if (command === 'undo_edit') return 'undo-edit';
    return 'edit';
  }
  if (eventType.includes('tasktracker')) return command === 'plan' ? 'task-update' : 'task-view';
  if (eventType.includes('applypatch')) return 'patch';
  if (eventType.includes('terminal') || eventType.includes('shell')) return 'command';
  if (eventType.includes('grep') || eventType.includes('glob') || eventType.includes('search')) return 'search';
  if (eventType.includes('browser') || eventType.includes('web')) return 'web';
  if (eventType.includes('delegate') || eventType === 'task' || eventType.includes('subagent')) return 'delegate';
  if (eventType.includes('workflow')) return 'workflow';
  if (eventType.includes('consulttom') || eventType.includes('sleeptimecompute') || eventType.includes('consult')) return 'consult';
  if (eventType.includes('think')) return 'thought';

  // Compatibility fallbacks for adapters that use descriptive event names.
  if (/(read|openfile|viewfile|catfile)/.test(eventType)) return 'read';
  if (/(write|edit|patch|replace|createfile)/.test(eventType)) return 'edit';
  if (/(command|terminal|shell|bash|execute|process)/.test(eventType)) return 'command';
  return 'generic';
}

function toolActivityStatus(tool: Record<string, unknown>, completed: boolean): ToolActivityStatus {
  if (!completed) return 'running';
  return eventDetails(tool.details).is_error === true ? 'failed' : 'completed';
}

function toolActivityLabel(tool: Record<string, unknown>, status: ToolActivityStatus) {
  const labels: Record<ToolActivityKind, Record<ToolActivityStatus, string>> = {
    read: { running: '正在读取文件', completed: '已读取文件', failed: '读取文件失败', ended: '读取已结束（无返回记录）' },
    create: { running: '正在创建文件', completed: '已创建文件', failed: '创建文件失败', ended: '创建已结束（无返回记录）' },
    edit: { running: '正在编辑文件', completed: '已编辑文件', failed: '编辑文件失败', ended: '编辑已结束（无返回记录）' },
    'undo-edit': { running: '正在撤销文件修改', completed: '已撤销文件修改', failed: '撤销文件修改失败', ended: '撤销已结束（无返回记录）' },
    patch: { running: '正在应用代码补丁', completed: '已应用代码补丁', failed: '应用代码补丁失败', ended: '补丁操作已结束（无返回记录）' },
    command: { running: '正在运行命令', completed: '已运行命令', failed: '命令运行失败', ended: '命令已结束（无返回记录）' },
    search: { running: '正在搜索内容', completed: '已搜索内容', failed: '搜索内容失败', ended: '搜索已结束（无返回记录）' },
    web: { running: '正在操作网页', completed: '已操作网页', failed: '网页操作失败', ended: '网页操作已结束（无返回记录）' },
    delegate: { running: '正在安排子 Agent', completed: '已安排子 Agent', failed: '安排子 Agent 失败', ended: '子 Agent 安排已结束（无返回记录）' },
    'task-view': { running: '正在查看任务列表', completed: '已查看任务列表', failed: '查看任务列表失败', ended: '查看已结束（无返回记录）' },
    'task-update': { running: '正在更新任务列表', completed: '已更新任务列表', failed: '更新任务列表失败', ended: '更新已结束（无返回记录）' },
    workflow: { running: '正在运行工作流', completed: '已运行工作流', failed: '工作流运行失败', ended: '工作流已结束（无返回记录）' },
    consult: { running: '正在咨询辅助 Agent', completed: '已获得辅助 Agent 建议', failed: '咨询辅助 Agent 失败', ended: '咨询已结束（无返回记录）' },
    thought: { running: '正在记录思考', completed: '已记录思考', failed: '记录思考失败', ended: '思考记录已结束（无返回记录）' },
    generic: { running: '正在使用工具', completed: '已使用工具', failed: '工具执行失败', ended: '工具操作已结束（无返回记录）' },
  };
  return labels[toolActivityKind(tool)][status];
}

function toolMatchIdentity(message: AgentMessage) {
  const tool = eventDetails(message.content.tool);
  const details = eventDetails(tool.details);
  const family = toolActivityKind(tool);
  const command = typeof details.command === 'string' ? details.command.trim() : '';
  const targetKeys = ['path', 'file_path', 'pattern', 'query', 'url', 'prompt', 'instruction', 'task_id'];
  const target = targetKeys.map(key => details[key]).find(value => typeof value === 'string' && value.trim());
  return {
    family,
    command,
    exact: `${family}\u0000${command}\u0000${typeof target === 'string' ? target.trim() : ''}`,
  };
}

function matchedToolCallStatuses(messages: AgentMessage[], groupRunning: boolean) {
  const statuses = new Map<string, ToolActivityStatus>();
  const results = new Map<string, AgentMessage>();
  const matchedResultIds = new Set<string>();
  const pending: Array<{ message: AgentMessage; family: ToolActivityKind; command: string; exact: string }> = [];

  for (const message of messages) {
    if (message.message_type === 'TOOL_CALL') {
      pending.push({ message, ...toolMatchIdentity(message) });
      continue;
    }
    if (message.message_type !== 'TOOL_RESULT') continue;

    const result = toolMatchIdentity(message);
    let matchIndex = pending.findIndex(call => call.exact === result.exact);
    if (matchIndex < 0 && result.command) {
      matchIndex = pending.findIndex(call => call.family === result.family && call.command === result.command);
    }
    if (matchIndex < 0) matchIndex = pending.findIndex(call => call.family === result.family);
    if (matchIndex < 0) continue;

    const [call] = pending.splice(matchIndex, 1);
    const resultTool = eventDetails(message.content.tool);
    statuses.set(call.message.id, toolActivityStatus(resultTool, true));
    results.set(call.message.id, message);
    matchedResultIds.add(message.id);
  }

  for (const call of pending) statuses.set(call.message.id, groupRunning ? 'running' : 'ended');
  return { statuses, results, matchedResultIds };
}

function hasPendingToolCall(messages: AgentMessage[]) {
  const pending: Array<{ family: ToolActivityKind; command: string; exact: string }> = [];

  for (const message of messages) {
    if (message.message_type === 'TOOL_CALL') {
      pending.push(toolMatchIdentity(message));
      continue;
    }
    if (message.message_type !== 'TOOL_RESULT') continue;

    const result = toolMatchIdentity(message);
    let matchIndex = pending.findIndex(call => call.exact === result.exact);
    if (matchIndex < 0 && result.command) {
      matchIndex = pending.findIndex(call => call.family === result.family && call.command === result.command);
    }
    if (matchIndex < 0) matchIndex = pending.findIndex(call => call.family === result.family);
    if (matchIndex >= 0) pending.splice(matchIndex, 1);
  }

  return pending.length > 0;
}

function shouldShowThinkingIndicator(conversation: AgentConversation | undefined, messages: AgentMessage[]) {
  if (conversation?.state !== 'GENERATING') return false;

  const activeMessages = messages.filter(message => message.delivery_state !== 'CANCELLED');
  const currentInput = [...activeMessages].reverse().find(message =>
    (message.source === 'HUMAN' || message.source === 'PROGRAM')
    && message.delivery_state === 'DELIVERED'
  );
  if (!currentInput) return false;

  const turnMessages = activeMessages.filter(message => message.sequence_no > currentInput.sequence_no);
  const hasFormalReply = turnMessages.some(message =>
    message.source === 'AGENT'
    && (
      message.message_type === 'ERROR'
      || (message.message_type === 'TEXT' && message.content.presentation !== 'progress')
    )
  );

  return !hasFormalReply && !hasPendingToolCall(turnMessages);
}

function activityPreview(tool: Record<string, unknown>, content: string, details: Record<string, unknown>) {
  const kind = toolActivityKind(tool);
  const preferred: Partial<Record<ToolActivityKind, string[]>> = {
    read: ['path', 'file_path'],
    create: ['path', 'file_path'],
    edit: ['path', 'file_path'],
    'undo-edit': ['path', 'file_path'],
    patch: ['path', 'file_path'],
    command: ['command'],
    search: ['pattern', 'query', 'search_path', 'path'],
    web: ['url'],
    delegate: ['prompt', 'instruction', 'task'],
  };
  for (const key of preferred[kind] ?? []) {
    const value = details[key];
    if (typeof value === 'string' && value.trim()) return value.trim().split('\n')[0];
  }
  if (kind === 'task-view' || kind === 'task-update') {
    const tasks = Array.isArray(details.task_list) ? details.task_list.length : 0;
    return tasks ? `${tasks} 个任务` : '';
  }
  return content.split('\n')[0] || (Object.keys(details).length ? `${Object.keys(details).length} 项详情` : '');
}

function SubagentSummary({ subagents }: { subagents: AgentConversation[] }) {
  if (!subagents.length) return null;
  const running = subagents.filter(item => !['READ_ONLY', 'FAILED'].includes(item.state)).length;
  const failed = subagents.filter(item => item.state === 'FAILED').length;
  const status = running ? `${subagents.length} · ${running} 运行中` : failed ? `${subagents.length} · ${failed} 失败` : `${subagents.length} 完成`;
  return <section className="subagent-summary"><h3>子智能体</h3><div><span className="subagent-avatars">{subagents.slice(0, 5).map((item, index) => {
    const state = item.state === 'FAILED' ? 'failed' : item.state === 'READ_ONLY' ? 'completed' : 'running';
    return <i key={item.id} className={state} title={`${item.title} · ${STATE_LABELS[item.state] ?? item.state}`} style={{ '--subagent-index': index } as React.CSSProperties}><Bot size={12}/></i>;
  })}</span><b>{status}</b></div></section>;
}

function ActivityMessage({ message, resultMessage, statusOverride }: { message: AgentMessage; resultMessage?: AgentMessage; statusOverride?: ToolActivityStatus }) {
  if (message.message_type === 'STATE') {
    const state = eventDetails(message.content.state);
    const text = typeof state.content === 'string' ? state.content.trim() : '';
    if (!text) return null;
    return <div className="activity-message thought-message"><Sparkles size={14}/><MarkdownMessage text={text} messageId={message.id}/></div>;
  }
  const tool = eventDetails(message.content.tool);
  const details = usefulDetails(tool.details);
  const content = typeof tool.content === 'string' ? tool.content.trim() : '';
  if (!content && !Object.keys(details).length && !tool.event_name) return null;
  const completed = message.message_type === 'TOOL_RESULT';
  const status = statusOverride ?? toolActivityStatus(tool, completed);
  const resultTool = eventDetails(resultMessage?.content.tool);
  const resultContent = typeof resultTool.content === 'string' ? resultTool.content.trim() : '';
  const resultDetails = usefulDetails(resultTool.details);
  return <details className={`activity-message tool-activity ${status}`}><summary><span>{status === 'failed' ? <XCircle size={14}/> : status === 'running' ? <LoaderCircle size={14}/> : status === 'ended' ? <Clock3 size={14}/> : <CheckCircle2 size={14}/>}<b>{toolActivityLabel(tool, status)}</b><small>{activityPreview(tool, content, details)}</small></span><ChevronRight size={14}/></summary><div className="activity-detail">{content && <MarkdownMessage text={content} messageId={message.id}/>} {!!Object.keys(details).length && <pre>{JSON.stringify(details, null, 2)}</pre>}{resultContent && resultContent !== content && <MarkdownMessage text={resultContent} messageId={resultMessage?.id}/>} {!!Object.keys(resultDetails).length && JSON.stringify(resultDetails) !== JSON.stringify(details) && <pre>{JSON.stringify(resultDetails, null, 2)}</pre>}</div></details>;
}

const isActivityMessage = (message: AgentMessage) => message.message_type === 'TOOL_CALL' || message.message_type === 'TOOL_RESULT' || message.message_type === 'STATE';

function activitySummary(message: AgentMessage) {
  if (message.message_type === 'STATE') {
    const state = eventDetails(message.content.state);
    const text = typeof state.content === 'string' ? state.content.trim().split('\n')[0] : '';
    return text ? `正在思考 · ${text}` : '正在分析上下文';
  }
  const tool = eventDetails(message.content.tool);
  return toolActivityLabel(tool, toolActivityStatus(tool, message.message_type === 'TOOL_RESULT'));
}

function formatDuration(start: string, end: string | number) {
  const seconds = Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return minutes ? `${minutes} 分 ${remaining} 秒` : `${remaining} 秒`;
}

function ActivityGroup({ messages, running, startedAt, completedAt }: { messages: AgentMessage[]; running: boolean; startedAt?: string; completedAt?: string }) {
  const [clock, setClock] = useState(Date.now());
  const [expanded, setExpanded] = useState(running);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  useEffect(() => setExpanded(running), [running]);
  const last = messages[messages.length - 1];
  const pairing = useMemo(() => matchedToolCallStatuses(messages, running), [messages, running]);
  const duration = formatDuration(
    startedAt ?? messages[0].created_at,
    running ? clock : completedAt ?? messages[messages.length - 1].created_at,
  );
  return <details className={`activity-group ${running ? 'running' : 'completed'}`} open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><span>{running ? <LoaderCircle size={14}/> : <CheckCircle2 size={14}/>}<b>{running ? '正在处理' : '查看处理过程'}</b><small>{duration}</small>{running && <em>{activitySummary(last)}</em>}</span><ChevronRight size={14}/></summary>
    <div className="activity-group-content">{messages.filter(message => !pairing.matchedResultIds.has(message.id)).map(message => <ActivityMessage key={message.id} message={message} resultMessage={pairing.results.get(message.id)} statusOverride={pairing.statuses.get(message.id)}/>)}</div>
  </details>;
}

function SubagentActivity({ subagents }: { subagents: AgentConversation[] }) {
  if (!subagents.length) return null;
  const running = subagents.filter(item => !['READ_ONLY', 'FAILED'].includes(item.state)).length;
  const failed = subagents.filter(item => item.state === 'FAILED').length;
  const label = running ? '子 Agent 已开始工作' : failed ? '子 Agent 工作遇到问题' : '子 Agent 已完成工作';
  return <details className={`subagent-activity ${running ? 'running' : failed ? 'failed' : 'completed'}`}>
    <summary><span className="subagent-capsule"><Bot size={13}/><b>{label}</b><small>{running ? `${running} 个运行中` : `${subagents.length} 个`}</small></span><ChevronRight size={14}/></summary>
    <div className="subagent-activity-list">{subagents.map(item => <article key={item.id}><span><Bot size={12}/></span><div><b>{conversationTitle(item.title)}</b>{item.delegation_instruction && <small>{item.delegation_instruction}</small>}</div><em>{STATE_LABELS[item.state] ?? item.state}</em></article>)}</div>
  </details>;
}

interface ActivityTimelineGroup { type: 'activity'; id: string; messages: AgentMessage[] }
interface MessageTimelineItem { type: 'message'; id: string; message: AgentMessage }

function groupTimelineMessages(messages: AgentMessage[]): Array<ActivityTimelineGroup | MessageTimelineItem> {
  const items: Array<ActivityTimelineGroup | MessageTimelineItem> = [];
  for (const message of messages) {
    if (!isActivityMessage(message)) {
      items.push({ type: 'message', id: message.id, message });
      continue;
    }
    const previous = items[items.length - 1];
    if (previous?.type === 'activity') {
      previous.messages.push(message);
    } else {
      items.push({ type: 'activity', id: message.id, messages: [message] });
    }
  }
  return items;
}

function deliveryLabel(message: AgentMessage, conversationState?: AgentConversation['state'], latestHuman = false) {
  if (message.delivery_state === 'QUEUED') return message.content.presentation === 'queued'
    ? '已加入队列 · 当前回合完成后发送'
    : '等待后台投递 · Agent 尚未收到';
  if (message.delivery_state === 'DELIVERING') return '正在发送给 Agent';
  if (message.delivery_state === 'DELIVERED') return latestHuman && conversationState === 'GENERATING'
    ? 'Agent 已收到 · 正在处理'
    : 'Agent 已收到';
  if (message.delivery_state === 'FAILED') return '投递失败';
  return '已取消';
}

function MessageBubble({ message, onRetry, onFork, onRevise, onPreview, final, conversationState, latestHuman, editable, forking, revising }: {
  message: AgentMessage;
  onRetry: (id: string) => void;
  onFork: (message: AgentMessage) => void;
  onRevise: (message: AgentMessage, text: string) => void;
  onPreview: (preview: AttachmentPreview) => void;
  final: boolean;
  conversationState?: AgentConversation['state'];
  latestHuman: boolean;
  editable: boolean;
  forking: boolean;
  revising: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [editedText, setEditedText] = useState(messageText(message));
  useEffect(() => {
    if (!editable) setEditing(false);
  }, [editable]);
  const meta = message.source === 'PROGRAM'
    ? { label: '流程自动发送', className: 'program' }
    : message.source === 'HUMAN'
      ? { label: '人工接管', className: 'human' }
      : { label: '模型回复', className: 'agent' };
  if (message.message_type === 'TOOL_CALL' || message.message_type === 'TOOL_RESULT' || message.message_type === 'STATE') return <ActivityMessage message={message}/>;
  if (message.message_type === 'ERROR') {
    return <article className="message-error"><XCircle size={17}/><div><b>执行遇到问题</b><p>{messageText(message)}</p></div></article>;
  }
  const text = messageText(message);
  const progress = message.content.presentation === 'progress';
  if (progress) return <article className="agent-progress-message"><MarkdownMessage text={text} messageId={message.id}/></article>;
  return <article
    className={`chat-message ${meta.className} ${message.source === 'PROGRAM' ? 'has-avatar' : 'no-avatar'} ${final ? 'final-answer' : ''}`}
    data-user-message-id={message.source === 'HUMAN' ? message.id : undefined}
  >
    {message.source === 'PROGRAM' && <div className="message-avatar" aria-label={meta.label} title={meta.label}><SourceIcon source={message.source}/></div>}
    <div className="message-body"><header>{message.source !== 'HUMAN' && <b>{final ? '最终答复' : meta.label}</b>}<span>{new Date(message.created_at).toLocaleTimeString()}</span></header>{!!message.content.capability_refs?.length && <div className="message-capability-refs">{message.content.capability_refs.map(item => <span key={`${item.capability_type}-${item.capability_key}`}>{item.capability_type === 'SKILL' ? 'Skill' : 'MCP'} · {item.capability_key}</span>)}</div>}{editing ? <div className="message-revision-editor"><textarea autoFocus aria-label="编辑最近发送的消息" value={editedText} maxLength={20000} onChange={event => setEditedText(event.target.value)}/><small>重新发送会在当前会话中替换这一轮上下文；原附件和能力引用保持不变。</small><div><button type="button" className="ghost" disabled={revising} onClick={() => { setEditedText(text); setEditing(false); }}>取消</button><button type="button" className="primary" disabled={revising || !editedText.trim() || editedText.trim() === text} onClick={() => onRevise(message, editedText.trim())}>{revising ? '正在重建…' : '重新发送'}</button></div></div> : <>{text && (message.source === 'AGENT' ? <MarkdownMessage text={text} messageId={message.id}/> : <p>{text}</p>)}<MessageAttachmentList message={message} onPreview={onPreview}/></>}
      {!editing && message.message_type === 'TEXT' && text && ((message.source === 'AGENT') || editable) && <div className="message-branch-actions">{editable && <button type="button" disabled={revising} onClick={() => { setEditedText(text); setEditing(true); }} title="编辑这条消息并在当前会话重新生成"><Pencil size={12}/>编辑重发</button>}{message.source === 'AGENT' && <button type="button" className="fork-action" disabled={forking} onClick={() => onFork(message)} title={forking ? '正在创建分支' : '从此分叉'} aria-label={forking ? '正在创建分支' : '从此分叉'}><GitFork size={14}/></button>}</div>}
      {message.source !== 'AGENT' && <footer className={`delivery ${message.delivery_state.toLowerCase()}`}>{deliveryLabel(message, conversationState, latestHuman)}{message.delivery_state === 'FAILED' && <>{message.error_detail && <span>{message.error_code === 'RUNTIME_CONVERSATION_MISSING' || message.error_code === 'EXECUTOR_UNAVAILABLE' ? 'Agent 连接已失效，重试会自动重建' : message.error_detail}</span>}<button onClick={() => onRetry(message.id)}><RefreshCw size={12}/>重试并重连</button></>}</footer>}
    </div>
  </article>;
}

function MessageTimeline({ conversation, messages, streamingText, subagents, onRetry, onFork, onRevise, onPreview, forkingId, revisingId, retrying }: {
  conversation?: AgentConversation;
  messages: AgentMessage[];
  streamingText?: string;
  subagents: AgentConversation[];
  onRetry: (id: string) => void;
  onFork: (message: AgentMessage) => void;
  onRevise: (message: AgentMessage, text: string) => void;
  onPreview: (preview: AttachmentPreview) => void;
  forkingId?: string;
  revisingId?: string;
  retrying: boolean;
}) {
  const timelineRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const navigatorRef = useRef<HTMLElement>(null);
  const messageMarkersRef = useRef<Array<{ id: string; position: number; scrollTop: number }>>([]);
  const scrubbingRef = useRef(false);
  const scrubStartYRef = useRef(0);
  const suppressMarkerClickRef = useRef(false);
  const followRef = useRef(true);
  const [following, setFollowing] = useState(true);
  const [messageMarkers, setMessageMarkers] = useState<Array<{ id: string; position: number; scrollTop: number }>>([]);
  const [activeMarkerId, setActiveMarkerId] = useState<string>();
  const [hoveredMarkerId, setHoveredMarkerId] = useState<string>();
  const [markerWavePosition, setMarkerWavePosition] = useState<number>();
  const [scrubbing, setScrubbing] = useState(false);
  const answering = conversation?.state === 'GENERATING' || conversation?.state === 'WAITING_SUBAGENTS';
  const showThinking = !streamingText && shouldShowThinkingIndicator(conversation, messages);
  const timelineItems = groupTimelineMessages(messages);
  const userMessages = useMemo(() => messages.filter(message => message.source === 'HUMAN'), [messages]);
  const userMessageById = useMemo(() => new Map(userMessages.map(message => [message.id, message])), [userMessages]);
  const lastActivityGroupId = [...timelineItems].reverse().find(item => item.type === 'activity')?.id;
  const latestInputId = [...messages].reverse().find(message => message.source === 'HUMAN' || message.source === 'PROGRAM')?.id;
  const version = messages.map(message => `${message.id}:${message.delivery_state}`).join('|');
  const scrollToBottom = useCallback(() => {
    const timeline = timelineRef.current;
    if (timeline) timeline.scrollTop = timeline.scrollHeight;
  }, []);
  const pauseFollowing = useCallback(() => {
    followRef.current = false;
    setFollowing(false);
  }, []);
  const updateActiveMarker = useCallback((scrollTop: number, markers = messageMarkersRef.current) => {
    if (!markers.length) {
      setActiveMarkerId(undefined);
      return;
    }
    let active = markers[0];
    for (const marker of markers) {
      if (marker.scrollTop > scrollTop + 48) break;
      active = marker;
    }
    setActiveMarkerId(active.id);
  }, []);
  const measureMessageMarkers = useCallback(() => {
    const timeline = timelineRef.current;
    const content = contentRef.current;
    if (!timeline || !content) return;
    const maxScroll = Math.max(0, timeline.scrollHeight - timeline.clientHeight);
    const contentTop = content.getBoundingClientRect().top;
    const nodes = [...content.querySelectorAll<HTMLElement>('[data-user-message-id]')];
    const measured = nodes.flatMap(node => {
      const id = node.dataset.userMessageId;
      if (!id) return [];
      const contentOffset = node.getBoundingClientRect().top - contentTop;
      const scrollTop = Math.min(maxScroll, Math.max(0, contentOffset - 24));
      return [{ id, scrollTop }];
    });
    // The navigator is an ordered index, not a transcript minimap. Keep ticks
    // tightly grouped and use their relative order only; scrollTop still points
    // to each message's real location.
    const markers = measured.map((marker, index) => ({
      ...marker,
      position: measured.length === 1 ? .5 : index / (measured.length - 1),
    }));
    messageMarkersRef.current = markers;
    setMessageMarkers(markers);
    updateActiveMarker(timeline.scrollTop, markers);
  }, [updateActiveMarker]);
  const updateFollowingFromScroll = useCallback((timeline: HTMLDivElement) => {
    updateActiveMarker(timeline.scrollTop);
    const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= 24;
    if (atBottom) {
      followRef.current = true;
      setFollowing(true);
    } else {
      pauseFollowing();
    }
  }, [pauseFollowing, updateActiveMarker]);
  const scrollToMarker = useCallback((markerId: string, behavior: ScrollBehavior = 'smooth') => {
    const timeline = timelineRef.current;
    const marker = messageMarkers.find(item => item.id === markerId);
    if (!timeline || !marker) return;
    pauseFollowing();
    setActiveMarkerId(marker.id);
    timeline.scrollTo({ top: marker.scrollTop, behavior });
  }, [messageMarkers, pauseFollowing]);
  const updateMarkerWave = useCallback((clientY: number) => {
    const navigator = navigatorRef.current;
    if (!navigator || !messageMarkers.length) return undefined;
    const bounds = navigator.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientY - bounds.top) / bounds.height));
    const closest = messageMarkers.reduce((nearest, marker) =>
      Math.abs(marker.position - ratio) < Math.abs(nearest.position - ratio) ? marker : nearest
    );
    setMarkerWavePosition(ratio);
    setHoveredMarkerId(closest.id);
    return closest;
  }, [messageMarkers]);
  const scrubTimeline = useCallback((clientY: number) => {
    const timeline = timelineRef.current;
    const closest = updateMarkerWave(clientY);
    if (!timeline || !closest) return;
    pauseFollowing();
    timeline.scrollTop = closest.scrollTop;
  }, [pauseFollowing, updateMarkerWave]);
  const resumeFollowing = useCallback(() => {
    followRef.current = true;
    setFollowing(true);
    scrollToBottom();
  }, [scrollToBottom]);

  useLayoutEffect(() => {
    followRef.current = true;
    setFollowing(true);
    scrollToBottom();
  }, [conversation?.id, scrollToBottom]);
  useLayoutEffect(() => {
    if (followRef.current) scrollToBottom();
    measureMessageMarkers();
  }, [conversation?.state, measureMessageMarkers, scrollToBottom, version]);
  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (followRef.current) scrollToBottom();
      measureMessageMarkers();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [conversation?.id, measureMessageMarkers, scrollToBottom]);
  useEffect(() => {
    const measure = () => measureMessageMarkers();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [measureMessageMarkers]);

  const previewMarkerMessage = hoveredMarkerId ? userMessageById.get(hoveredMarkerId) : undefined;

  return <div className="message-timeline-shell"><div
      ref={timelineRef}
      className="message-timeline"
      aria-live="polite"
      aria-label="对话消息"
      onScroll={event => updateFollowingFromScroll(event.currentTarget)}
    ><div ref={contentRef} className="message-timeline-content">
    {timelineItems.map(item => {
      if (item.type === 'activity') {
        const firstSequence = item.messages[0].sequence_no;
        const lastSequence = item.messages[item.messages.length - 1].sequence_no;
        const startedBy = [...messages].reverse().find(message => message.sequence_no < firstSequence && (message.source === 'HUMAN' || message.source === 'PROGRAM'));
        const completedBy = messages.find(message => message.sequence_no > lastSequence && message.source === 'AGENT' && message.message_type === 'TEXT');
        const running = answering && item.id === lastActivityGroupId && !completedBy && hasPendingToolCall(item.messages);
        return <ActivityGroup key={item.id} messages={item.messages} running={running} startedAt={startedBy?.created_at} completedAt={completedBy?.created_at ?? item.messages[item.messages.length - 1].created_at}/>;
      }
      const message = item.message;
      return <MessageBubble key={item.id} message={message} onRetry={onRetry} onFork={onFork} onRevise={onRevise} onPreview={onPreview} editable={conversation?.editable_message_id === message.id} forking={message.id === forkingId} revising={message.id === revisingId} final={message.content.presentation === 'final'} conversationState={conversation?.state} latestHuman={message.id === latestInputId}/>;
    })}
    {streamingText && <article className="agent-progress-message streaming" aria-live="polite"><MarkdownMessage text={streamingText} messageId={`stream-${conversation?.id ?? 'agent'}`}/><span className="streaming-caret" aria-hidden="true"/></article>}
    <SubagentActivity subagents={subagents}/>
    {showThinking && <div className="agent-thinking-indicator" role="status" aria-label="Agent 正在思考"><span>正在思考</span></div>}
    {conversation && <AgentActivityStatus conversation={conversation} messages={messages} retrying={retrying} onRetry={onRetry}/>}
    {conversation && !messages.length && <div className="conversation-empty"><Bot size={26}/><b>当前轮次上下文已挂载</b><span>发送第一条消息开始协作。</span></div>}
    {!conversation && <div className="conversation-empty"><Workflow size={26}/><b>尚无可用会话</b><span>本轮开始执行后将自动创建默认会话。</span></div>}
    </div></div>{messageMarkers.length > 0 && <nav
      ref={navigatorRef}
      className={`message-position-navigator ${markerWavePosition === undefined ? '' : 'interacting'} ${scrubbing ? 'scrubbing' : ''}`}
      style={{ '--marker-count': messageMarkers.length } as React.CSSProperties}
      aria-label="用户消息定位"
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
        scrubbingRef.current = true;
        scrubStartYRef.current = event.clientY;
        suppressMarkerClickRef.current = false;
        setScrubbing(true);
        scrubTimeline(event.clientY);
      }}
      onPointerMove={event => {
        if (scrubbingRef.current && event.currentTarget.hasPointerCapture(event.pointerId)) {
          if (Math.abs(event.clientY - scrubStartYRef.current) > 3) suppressMarkerClickRef.current = true;
          scrubTimeline(event.clientY);
        } else updateMarkerWave(event.clientY);
      }}
      onPointerUp={event => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
        scrubbingRef.current = false;
        setScrubbing(false);
        if (event.pointerType !== 'mouse') {
          setHoveredMarkerId(undefined);
          setMarkerWavePosition(undefined);
        }
        window.requestAnimationFrame(() => { suppressMarkerClickRef.current = false; });
      }}
      onPointerCancel={() => { scrubbingRef.current = false; setScrubbing(false); setHoveredMarkerId(undefined); setMarkerWavePosition(undefined); }}
      onPointerLeave={() => { if (!scrubbingRef.current) { setHoveredMarkerId(undefined); setMarkerWavePosition(undefined); } }}
    >
      {messageMarkers.map((marker, index) => {
        const message = userMessageById.get(marker.id);
        const waveCenter = markerWavePosition === undefined ? -1 : markerWavePosition * Math.max(0, messageMarkers.length - 1);
        const waveDistance = Math.abs(index - waveCenter);
        const waveStrength = waveDistance >= 4 ? 0 : Math.cos((waveDistance / 4) * (Math.PI / 2)) ** 2;
        return <button
          type="button"
          key={marker.id}
          className={marker.id === activeMarkerId ? 'selected' : ''}
          style={{ top: `${marker.position * 100}%`, '--marker-wave': waveStrength } as React.CSSProperties}
          aria-label={`定位到第 ${index + 1} 条用户消息：${message ? messageSummary(message) : ''}`}
          aria-current={marker.id === activeMarkerId ? 'location' : undefined}
          onFocus={() => { setHoveredMarkerId(marker.id); setMarkerWavePosition(marker.position); }}
          onBlur={() => { setHoveredMarkerId(undefined); setMarkerWavePosition(undefined); }}
          onClick={event => {
            if (suppressMarkerClickRef.current) { event.preventDefault(); return; }
            scrollToMarker(marker.id);
          }}
        ><span/></button>;
      })}
      {previewMarkerMessage && <div
        className="message-position-preview"
        style={{ top: `clamp(44px, ${(messageMarkers.find(marker => marker.id === hoveredMarkerId)?.position ?? 0) * 100}%, calc(100% - 44px))` }}
      ><b>{messageSummary(previewMarkerMessage)}</b><span>{new Date(previewMarkerMessage.created_at).toLocaleTimeString()} · 用户消息</span></div>}
    </nav>}{!following && <button
      type="button"
      className={`timeline-follow-button ${answering ? 'answering' : 'answered'}`}
      onWheel={event => {
        event.preventDefault();
        event.stopPropagation();
        timelineRef.current?.scrollBy({ top: event.deltaY });
      }}
      onClick={resumeFollowing}
    >{answering ? <LoaderCircle size={14}/> : <CornerDownRight size={14}/>}<span>{answering ? '正在回答 · 跟踪最新' : '立即跟踪最新回答'}</span></button>}</div>;
}

function AgentActivityStatus({ conversation, messages, retrying, onRetry }: { conversation: AgentConversation; messages: AgentMessage[]; retrying: boolean; onRetry: (id: string) => void }) {
  const activeMessages = messages.filter(message => message.delivery_state !== 'CANCELLED');
  const waitingDelivery = [...activeMessages].reverse().find(message => message.delivery_state === 'QUEUED' && message.content.presentation !== 'queued');
  const delivering = [...activeMessages].reverse().find(message => message.delivery_state === 'DELIVERING');
  let tone = 'working';
  let title = '';
  let detail = '';
  let icon = <LoaderCircle size={15}/>;

  if (conversation.state === 'CREATING') {
    const status = conversation.connection_status;
    const phases = {
      WAITING_WORKER: ['等待后台调度', '消息已保存，正在等待 Worker 接手创建任务。'],
      PREPARING_CONTEXT: ['正在准备执行上下文', '正在整理节点快照、输入绑定、模型与能力配置。'],
      STARTING_RUNTIME: ['正在启动 Agent 运行环境', '正在创建隔离容器并等待 Agent Server 就绪。'],
      CONNECTING_AGENT: ['正在创建 Agent 会话', '运行环境已经就绪，正在建立 Agent 会话。'],
      READY: ['Agent 已连接', 'Agent 会话已经可以接收消息。'],
      FAILED: ['Agent 连接失败', status?.detail || '运行环境或 Agent 会话创建失败。'],
    } as const;
    const phase = phases[status?.phase ?? 'WAITING_WORKER'];
    title = phase[0];
    detail = `${phase[1]}${status?.elapsed_seconds ? ` 已等待 ${status.elapsed_seconds} 秒。` : ''}`;
    if (status?.phase === 'FAILED') { tone = 'failed'; icon = <AlertTriangle size={15}/>; }
  } else if (conversation.state === 'STOPPING') {
    title = '正在强制终止会话';
    detail = '已提交停止请求，后台正在中断 Agent 并回收运行容器。';
  } else if (conversation.state === 'FAILED') {
    tone = 'failed';
    title = 'Agent 连接中断';
    detail = '消息仍然保留。可继续输入新消息自动重建，或重试失败消息。';
    icon = <AlertTriangle size={15}/>;
  } else if (waitingDelivery) {
    tone = 'waiting';
    title = '等待后台投递';
    detail = '消息已保存，但 Agent 和大模型尚未收到。如果持续停留在这里，可以重新投递。';
    icon = <Clock3 size={15}/>;
  } else if (delivering) {
    title = '正在发送给 Agent';
    detail = '后台正在把消息写入 Agent 会话，大模型尚未开始处理。';
  } else if (conversation.state === 'GENERATING') {
    // Generating is represented inline by either the thinking indicator or the
    // concrete tool activity group. Avoid a second diagnostic status card.
    return null;
  } else if (conversation.state === 'WAITING_HUMAN') {
    tone = 'waiting';
    title = 'Agent 正在等待你的回复';
    detail = '补充信息后，本轮执行会从当前上下文继续。';
    icon = <Clock3 size={15}/>;
  } else if (conversation.state === 'WAITING_SUBAGENTS') {
    title = '正在等待子 Agent 完成';
    detail = '子 Agent 正在并行处理已分配的任务，结果会自动回到当前对话。';
  } else {
    return null;
  }

  return <section className={`agent-activity-status ${tone}`} role="status"><span className="agent-activity-icon">{icon}</span><div><b>{title}</b><small>{detail}</small></div>{waitingDelivery && <button type="button" disabled={retrying} onClick={() => onRetry(waitingDelivery.id)}><RefreshCw size={12}/>{retrying ? '正在恢复…' : '重新投递'}</button>}</section>;
}

function ConversationRail({ conversations, selectedId, attempt, onSelect, onCreate, onDelete, creating, deleting }: {
  conversations: AgentConversation[]; selectedId?: string; attempt: NodeAttempt;
  onSelect: (id: string) => void; onCreate: () => void; onDelete: (item: AgentConversation) => void; creating: boolean; deleting?: string;
}) {
  const canCreate = STARTED_STATES.has(attempt.state);
  return <aside className="conversation-rail"><header><div><span className="eyebrow">CONVERSATIONS</span><h2>本轮协作会话</h2></div><button className="secondary icon-button" aria-label="新建会话" title={canCreate ? '新建会话' : '本轮开始执行后可新建'} disabled={!canCreate || creating} onClick={onCreate}><Plus size={16}/></button></header>
    <div className="conversation-list">{conversations.map(item => <div className={`conversation-list-item ${item.id === selectedId ? 'active' : ''}`} key={item.id}><button className="conversation-select" onClick={() => onSelect(item.id)}><span className={`conversation-kind ${item.kind === 'AUTO' ? 'auto' : 'human'}`}>{item.kind === 'AUTO' ? <Workflow size={13}/> : <HumanControlIcon size={14}/>}</span><span><b>{conversationTitle(item.title)}</b><small>{STATE_LABELS[item.state] ?? item.state} · {item.message_count} 条消息</small></span>{item.state === 'WAITING_HUMAN' && <i/>}</button>{item.kind === 'HUMAN_CREATED' && <button className="conversation-delete" aria-label={`删除 ${conversationTitle(item.title)}`} title="删除会话" disabled={deleting === item.id} onClick={() => onDelete(item)}>{deleting === item.id ? <RefreshCw size={13}/> : <Trash2 size={13}/>}</button>}</div>)}</div>
    {!conversations.length && <div className="conversation-empty"><Clock3 size={22}/><b>暂无会话</b><span>{canCreate ? '新建人工会话开始协作。' : '本轮开始执行后会自动建立默认会话。'}</span></div>}
  </aside>;
}

function RuntimeSelectionMenu({ models, modelName, reasoningEffort, disabled, onChange }: {
  models: ProviderModel[]; modelName: string; reasoningEffort: string; disabled: boolean;
  onChange: (selection: { modelName: string; reasoningEffort: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState<'model' | 'effort'>('model');
  const rootRef = useRef<HTMLDivElement>(null);
  const profile = models.find(item => item.model_name === modelName);
  const efforts = profile?.supported_reasoning_efforts ?? [];
  const defaultEffort = profile?.default_reasoning_effort ?? '';
  const effortLabel = reasoningEffort || defaultEffort || '模型默认';

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!rootRef.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape); };
  }, [open]);

  const selectModel = (nextModel: string) => {
    const nextEfforts = models.find(item => item.model_name === nextModel)?.supported_reasoning_efforts ?? [];
    onChange({ modelName: nextModel, reasoningEffort: nextEfforts.includes(reasoningEffort) ? reasoningEffort : '' });
    setOpen(false);
  };
  const selectEffort = (nextEffort: string) => { onChange({ modelName, reasoningEffort: nextEffort }); setOpen(false); };

  return <div className="runtime-selection-menu" ref={rootRef}>
    <button type="button" className="runtime-selection-trigger" aria-label="模型与推理强度" aria-haspopup="menu" aria-expanded={open} disabled={disabled || !models.length} onClick={() => setOpen(value => !value)}><span>{modelName || '未配置模型'}</span><i>·</i><span>{effortLabel}</span><ChevronDown size={13}/></button>
    {open && <div className="runtime-selection-popover">
      <div className="runtime-selection-main" role="menu">
        <button type="button" className={submenu === 'model' ? 'active' : ''} role="menuitem" onMouseEnter={() => setSubmenu('model')} onClick={() => setSubmenu('model')}><span><b>模型</b><small>{modelName || '未配置'}</small></span><ChevronRight size={14}/></button>
        <button type="button" className={submenu === 'effort' ? 'active' : ''} role="menuitem" disabled={!efforts.length} onMouseEnter={() => efforts.length && setSubmenu('effort')} onClick={() => efforts.length && setSubmenu('effort')}><span><b>推理强度</b><small>{effortLabel}</small></span><ChevronRight size={14}/></button>
      </div>
      <div className="runtime-selection-submenu" role="menu" aria-label={submenu === 'model' ? '选择模型' : '选择推理强度'}>
        {submenu === 'model' ? models.map(model => <button type="button" role="menuitemradio" aria-checked={model.model_name === modelName} key={model.model_name} onClick={() => selectModel(model.model_name)}><span>{model.model_name}</span>{model.model_name === modelName && <Check size={14}/>}</button>) : <>
          <button type="button" role="menuitemradio" aria-checked={!reasoningEffort} onClick={() => selectEffort('')}><span>模型默认{defaultEffort ? ` · ${defaultEffort}` : ''}</span>{!reasoningEffort && <Check size={14}/>}</button>
          {efforts.map(effort => <button type="button" role="menuitemradio" aria-checked={effort === reasoningEffort} key={effort} onClick={() => selectEffort(effort)}><span>{effort}</span>{effort === reasoningEffort && <Check size={14}/>}</button>)}
        </>}
      </div>
    </div>}
  </div>;
}

function ContextPanel({ attempt, nodeName, node, runName, messages, subagents, conversation, onPreview }: { attempt: NodeAttempt; nodeName: string; node?: { asset: { executor: { model_name?: string | null } | null; workspace_ref?: string; capabilities: Array<{ capability_type: string; capability_key: string }> } }; runName: string; messages: AgentMessage[]; subagents: AgentConversation[]; conversation?: AgentConversation; onPreview: (preview: AttachmentPreview) => void }) {
  const attachments = messages.flatMap(message => messageAttachments(message).map(item => ({ message, item })));
  const urls = [...new Set(messages.filter(message => message.source === 'HUMAN').flatMap(message => messageText(message).match(/https?:\/\/[^\s<>"']+/g) ?? []).map(value => value.replace(/[()[\]{},.;:!?，。；：！？]+$/, '')))];
  const collaboration = conversation?.kind === 'HUMAN_CREATED';
  return <aside className="conversation-context"><header><span className="eyebrow">EXECUTION CONTEXT</span><h2>执行上下文</h2></header><section><h3>当前事实</h3><dl><dt>流程运行</dt><dd>{runName}</dd><dt>节点</dt><dd>{nodeName}</dd><dt>执行轮次</dt><dd>第 {attempt.attempt_no} 轮</dd><dt>状态 / 版本</dt><dd>{attempt.state} · v{attempt.state_version}</dd></dl></section><section><h3>模型与能力</h3><dl><dt>模型</dt><dd>{conversation?.model_name || node?.asset.executor?.model_name || '服务默认'}</dd><dt>思考程度</dt><dd>{conversation?.reasoning_effort || '模型默认'}</dd><dt>能力策略</dt><dd>{collaboration ? '按消息动态选择' : '按会话动态选择'}</dd><dt>宿主工作区</dt><dd>{node?.asset.workspace_ref ? `var/workspaces/${node.asset.workspace_ref}` : '—'}</dd></dl>{node?.asset.capabilities.map(item => <span className="context-capability" key={`${item.capability_type}-${item.capability_key}`}>{item.capability_type} · {item.capability_key}</span>)}</section><SubagentSummary subagents={subagents}/><section><h3>会话来源</h3>{attachments.map(({ message, item }) => <article className="conversation-source" key={`${message.id}-${item.attachment_id}`}><button type="button" title={`预览 ${item.filename}`} onClick={() => onPreview({ messageId: message.id, item })}><span>{item.mime_type.startsWith('image/') ? <ImageIcon size={14}/> : <FileIcon size={14}/>}</span><span><b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small></span></button><a href={messageAttachmentUrl(message.id, item.attachment_id, true)} title={`下载 ${item.filename}`} aria-label={`下载 ${item.filename}`}><Download size={12}/></a></article>)}{urls.map(url => <a className="conversation-source url" key={url} href={url} target="_blank" rel="noreferrer"><span><Link2 size={14}/></span><b>{url}</b><small>URL</small></a>)}{!attachments.length && !urls.length && <p className="context-empty">消息中的附件和 URL 会集中显示在这里。</p>}</section><section><h3>输入与本轮产物</h3>{attempt.input_bindings.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.input_field_key}</span>)}{attempt.artifacts.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.field_key} · v{item.version_no}<small>{item.artifact_type}</small></span>)}</section><footer>{collaboration ? '人工会话共享本轮事实与能力，但不继承其他会话的启动任务。' : '这些会话只属于当前执行轮次；退回后产生的新轮次会建立独立上下文并保留这里的历史。'}</footer></aside>;
}

export function AgentChatPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId, selectConversation, returnToWorkbench } = useWorkbenchStore();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pendingAttachments, setPendingAttachments] = useState<Record<string, PendingAttachment[]>>({});
  const [runtimeSelections, setRuntimeSelections] = useState<Record<string, { modelName: string; reasoningEffort: string }>>({});
  const [attachmentError, setAttachmentError] = useState('');
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [runtimeSidebarCollapsed, setRuntimeSidebarCollapsed] = useState(false);
  const [attachmentPreview, setAttachmentPreview] = useState<AttachmentPreview>();
  const [streamingReplies, setStreamingReplies] = useState<Record<string, { text: string; complete: boolean }>>({});
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const runQuery = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId) });
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers });
  const attempt = useMemo(() => runQuery.data?.node_runs.find(item => item.id === selectedNodeRunId)?.attempts.find(item => item.id === selectedAttemptId), [runQuery.data, selectedAttemptId, selectedNodeRunId]);
  const conversationsQuery = useQuery({ queryKey: ['attempt-conversations', selectedAttemptId], queryFn: () => api.conversations(selectedAttemptId!), enabled: Boolean(selectedAttemptId), refetchInterval: 2500 });
  const conversations = conversationsQuery.data ?? [];
  const selected = conversations.find(item => item.id === selectedConversationId) ?? conversations[0];
  const messagesQuery = useQuery({ queryKey: ['conversation-messages', selected?.id], queryFn: () => api.conversationMessages(selected!.id), enabled: Boolean(selected), refetchInterval: selected?.state === 'GENERATING' || selected?.state === 'CREATING' || selected?.state === 'STOPPING' ? 1500 : 4000 });
  const subagentsQuery = useQuery({ queryKey: ['conversation-subagents', selected?.id], queryFn: () => api.conversationSubagents(selected!.id), enabled: Boolean(selected), refetchInterval: selected?.state === 'WAITING_SUBAGENTS' ? 1500 : 4000 });
  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['attempt-conversations', selectedAttemptId] });
    if (selected?.id) {
      void qc.invalidateQueries({ queryKey: ['conversation-messages', selected.id] });
      void qc.invalidateQueries({ queryKey: ['conversation-subagents', selected.id] });
    }
    if (selectedRunId) void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] });
  }, [qc, selected?.id, selectedAttemptId, selectedRunId]);
  useEffect(() => selectedRunId ? subscribeToRun(selectedRunId, refresh) : undefined, [refresh, selectedRunId]);
  useEffect(() => {
    if (!selected?.id || !selected.runtime_conversation_id) return undefined;
    return subscribeToConversationStream(selected.id, event => {
      if (event.type === 'delta' && event.content) {
        setStreamingReplies(current => {
          const previous = current[selected.id];
          return {
            ...current,
            [selected.id]: {
              text: `${previous?.complete ? '' : previous?.text ?? ''}${event.content}`,
              complete: false,
            },
          };
        });
      } else if (event.type === 'message_complete') {
        setStreamingReplies(current => current[selected.id]
          ? { ...current, [selected.id]: { ...current[selected.id], complete: true } }
          : current);
        refresh();
      }
    });
  }, [refresh, selected?.id, selected?.runtime_conversation_id]);
  const durableMessageVersion = (messagesQuery.data ?? []).map(message => `${message.id}:${message.delivery_state}`).join('|');
  useEffect(() => {
    if (!selected?.id) return;
    setStreamingReplies(current => current[selected.id]?.complete
      ? Object.fromEntries(Object.entries(current).filter(([id]) => id !== selected.id))
      : current);
  }, [durableMessageVersion, selected?.id]);
  useEffect(() => { if (selected && selected.id !== selectedConversationId) selectConversation(selected.id); }, [selectConversation, selected, selectedConversationId]);
  const createMutation = useMutation({ mutationFn: () => api.createConversation(attempt!.id, attempt!.state_version), onSuccess: item => { selectConversation(item.id); refresh(); } });
  const deleteMutation = useMutation({ mutationFn: api.deleteConversation, onSuccess: (_, deletedId) => { const next = conversations.find(item => item.id !== deletedId); qc.removeQueries({ queryKey: ['conversation-messages', deletedId] }); if (next) selectConversation(next.id); refresh(); } });
  const sendMutation = useMutation({
    mutationFn: async ({ conversation, content, capabilityRefs, attachments, runtime }: { conversation: AgentConversation; content: string; capabilityRefs: Array<{ capability_type: 'SKILL' | 'MCP'; capability_key: string }>; attachments: PendingAttachment[]; runtime: { model_name?: string; reasoning_effort?: string | null } }): Promise<AgentMessage | undefined> => {
      if (conversation.kind === 'AUTO' && attempt!.state === 'WAITING_HUMAN') {
        if (attachments.length) throw new Error('流程等待人工回复时暂不能附加文件，请先在人工会话中发送附件。');
        await api.humanInput(attempt!.id, content, attempt!.state_version, runtime);
        return undefined;
      }
      return api.sendConversationMessage(conversation.id, content, conversation.state_version, capabilityRefs, attachments, runtime);
    },
    onSuccess: (message, variables) => {
      setDrafts(old => ({ ...old, [variables.conversation.id]: '' }));
      setPendingAttachments(old => ({ ...old, [variables.conversation.id]: [] }));
      if (message) {
        qc.setQueryData<AgentMessage[]>(['conversation-messages', variables.conversation.id], current => current?.some(item => item.id === message.id) ? current.map(item => item.id === message.id ? message : item) : [...(current ?? []), message]);
        if (message.conversation_state_version) qc.setQueryData<AgentConversation[]>(['attempt-conversations', selectedAttemptId], current => current?.map(item => item.id === variables.conversation.id ? { ...item, state_version: message.conversation_state_version! } : item));
      }
      refresh();
    },
  });
  const retryMutation = useMutation({ mutationFn: api.retryConversationMessage, onSuccess: refresh });
  const steerMutation = useMutation({
    mutationFn: api.steerConversationMessage,
    onSuccess: message => {
      qc.setQueryData<AgentMessage[]>(['conversation-messages', message.conversation_id], current => current?.map(item => item.id === message.id ? message : item));
      refresh();
    },
  });
  const cancelQueuedMutation = useMutation({
    mutationFn: api.cancelQueuedConversationMessage,
    onSuccess: message => {
      qc.setQueryData<AgentMessage[]>(['conversation-messages', message.conversation_id], current => current?.map(item => item.id === message.id ? message : item));
      refresh();
    },
  });
  const forkMutation = useMutation({
    mutationFn: ({ message }: { message: AgentMessage }) => {
      if (!selected) throw new Error('当前没有可分叉的会话。');
      return api.forkConversationMessage(message.id, selected.state_version);
    },
    onSuccess: conversation => {
      qc.setQueryData<AgentConversation[]>(['attempt-conversations', selectedAttemptId], current => current?.some(item => item.id === conversation.id) ? current : [...(current ?? []), conversation]);
      selectConversation(conversation.id);
      refresh();
    },
  });
  const reviseMutation = useMutation({
    mutationFn: ({ message, text }: { message: AgentMessage; text: string }) => {
      if (!selected) throw new Error('当前没有可编辑的会话。');
      return api.reviseConversationMessage(message.id, selected.state_version, text);
    },
    onSuccess: conversation => {
      qc.setQueryData<AgentConversation[]>(['attempt-conversations', selectedAttemptId], current => current?.map(item => item.id === conversation.id ? conversation : item));
      refresh();
    },
  });
  const stopMutation = useMutation({
    mutationFn: async (conversation: AgentConversation) => conversation.kind === 'AUTO'
      ? api.cancelAttempt(attempt!.id, attempt!.state_version)
      : api.stopConversation(conversation.id, conversation.state_version),
    onSuccess: (result, conversation) => {
      if (conversation.kind === 'HUMAN_CREATED' && 'kind' in result) {
        qc.setQueryData<AgentConversation[]>(['attempt-conversations', selectedAttemptId], current => current?.map(item => item.id === result.id ? result : item));
      }
      refresh();
    },
  });
  if (!selectedRunId || !selectedNodeRunId || !selectedAttemptId) return <div className="empty"><b>缺少运行上下文</b><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div>;
  if (!runQuery.data || !attempt) return <div className="empty">加载 Agent 协作空间…</div>;
  const nodeRun = runQuery.data.node_runs.find(item => item.id === selectedNodeRunId)!;
  const snapshot = runQuery.data.snapshots.find(item => item.id === attempt.snapshot_id);
  const node = snapshot?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  const nodeName = node?.alias || node?.asset.name || nodeRun.flow_node_snapshot_key;
  const draft = selected ? drafts[selected.id] ?? '' : '';
  const attachments = selected ? pendingAttachments[selected.id] ?? [] : [];
  const provider = (providersQuery.data ?? []).find(item => item.id === node?.asset.executor?.model_provider_id);
  const enabledModels = provider?.models.filter(item => item.enabled) ?? [];
  const fallbackModel = selected?.model_name || node?.asset.executor?.model_name || enabledModels.find(item => item.is_default)?.model_name || enabledModels[0]?.model_name || '';
  const runtimeSelection = selected ? runtimeSelections[selected.id] ?? { modelName: fallbackModel, reasoningEffort: selected.reasoning_effort ?? '' } : { modelName: '', reasoningEffort: '' };
  const setRuntimeSelection = (next: { modelName: string; reasoningEffort: string }) => { if (selected) setRuntimeSelections(old => ({ ...old, [selected.id]: next })); };
  const callableCapabilities = (node?.asset.capabilities ?? []).filter(item => item.capability_type === 'SKILL' || item.capability_type === 'MCP');
  const selectedCapabilityKeys = callableCapabilities.filter(item => new RegExp(`(^|\\s)${escapeRegExp(capabilityMarker(item.capability_type, item.capability_key))}(?=\\s|$)`).test(draft)).map(item => `${item.capability_type}:${item.capability_key}`);
  const commandMatch = draft.match(/(^|\s)(\$)([^\s$]*)$/);
  const commandQuery = commandMatch?.[3]?.toLowerCase() ?? '';
  const commandSuggestions = commandMatch ? callableCapabilities.filter(item => item.capability_key.toLowerCase().includes(commandQuery)) : [];
  const readOnly = selected?.state === 'READ_ONLY';
  const queuedMessages = (messagesQuery.data ?? []).filter(message => message.source === 'HUMAN' && message.delivery_state === 'QUEUED' && message.content.presentation === 'queued');
  const timelineMessages = (messagesQuery.data ?? []).filter(message => !queuedMessages.some(queued => queued.id === message.id) && !(message.source === 'HUMAN' && message.delivery_state === 'CANCELLED' && message.content.presentation === 'cancelled-queue'));
  const replyingToAttempt = selected?.kind === 'AUTO' && attempt.state === 'WAITING_HUMAN';
  const addFiles = async (files: File[]) => {
    if (!selected || !files.length) return;
    setAttachmentError('');
    const existing = pendingAttachments[selected.id] ?? [];
    if (existing.length + files.length > MAX_ATTACHMENT_COUNT) { setAttachmentError(`每条消息最多添加 ${MAX_ATTACHMENT_COUNT} 个附件。`); return; }
    if (files.some(file => file.size > MAX_ATTACHMENT_BYTES)) { setAttachmentError('单个附件不能超过 10 MB。'); return; }
    if (existing.reduce((sum, item) => sum + item.byte_size, 0) + files.reduce((sum, file) => sum + file.size, 0) > MAX_ATTACHMENTS_BYTES) { setAttachmentError('单条消息的附件总大小不能超过 20 MB。'); return; }
    try {
      const prepared = await Promise.all(files.map(prepareBrowserAttachment));
      setPendingAttachments(old => ({ ...old, [selected.id]: [...(old[selected.id] ?? []), ...prepared] }));
    } catch (reason) { setAttachmentError(reason instanceof Error ? reason.message : '读取附件失败'); }
  };
  const removeAttachment = (id: string) => { if (selected) setPendingAttachments(old => ({ ...old, [selected.id]: (old[selected.id] ?? []).filter(item => item.id !== id) })); };
  const send = () => { const content = draft.trim(); if (selected && (content || attachments.length) && !readOnly) { const capabilityRefs = callableCapabilities.filter(item => selectedCapabilityKeys.includes(`${item.capability_type}:${item.capability_key}`)).map(item => ({ capability_type: item.capability_type as 'SKILL' | 'MCP', capability_key: item.capability_key })); sendMutation.mutate({ conversation: selected, content, capabilityRefs, attachments, runtime: { model_name: runtimeSelection.modelName || undefined, reasoning_effort: runtimeSelection.reasoningEffort || null } }); } };
  const stop = () => {
    if (!selected || selected.state !== 'GENERATING') return;
    stopMutation.mutate(selected);
  };
  const forkFrom = (message: AgentMessage) => {
    void dialog.confirm({
      title: '从此消息创建新会话？',
      message: '该消息及之前的历史会被复制到独立会话；原会话不会改变。',
      confirmLabel: '创建分支',
    }).then(confirmed => { if (confirmed) forkMutation.mutate({ message }); });
  };
  const selectCapability = (type: string, key: string) => { if (!selected) return; const marker = capabilityMarker(type, key); let next = draft; if (commandMatch) { const markerStart = (commandMatch.index ?? 0) + commandMatch[1].length; next = `${draft.slice(0, markerStart)}${marker} `; } else if (!new RegExp(`(^|\\s)${escapeRegExp(marker)}(?=\\s|$)`).test(draft)) { next = `${draft}${draft && !draft.endsWith(' ') ? ' ' : ''}${marker} `; } setDrafts(old => ({ ...old, [selected.id]: next })); setSuggestionIndex(0); };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (commandSuggestions.length) { if (event.key === 'ArrowDown') { event.preventDefault(); setSuggestionIndex(index => (index + 1) % commandSuggestions.length); return; } if (event.key === 'ArrowUp') { event.preventDefault(); setSuggestionIndex(index => (index - 1 + commandSuggestions.length) % commandSuggestions.length); return; } if (event.key === 'Escape') { event.preventDefault(); setSuggestionIndex(0); return; } if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); const item = commandSuggestions[Math.min(suggestionIndex, commandSuggestions.length - 1)]; selectCapability(item.capability_type, item.capability_key); return; } } if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } };
  return <section className="agent-chat-page"><div className="conversation-topbar"><button onClick={returnToWorkbench}><ArrowLeft size={15}/>返回运行详情</button><div><span>{runQuery.data.name}</span><b>{nodeName} · 第 {attempt.attempt_no} 轮</b></div><span className={`conversation-status ${selected?.state.toLowerCase() ?? ''}`}>{selected ? STATE_LABELS[selected.state] : '等待会话'}</span></div>
    <div className={`agent-chat-layout ${runtimeSidebarCollapsed ? 'runtime-sidebar-collapsed' : ''}`}><ConversationRail conversations={conversations} selectedId={selected?.id} attempt={attempt} onSelect={selectConversation} onCreate={() => createMutation.mutate()} onDelete={item => { void dialog.confirm({ title: `删除“${conversationTitle(item.title)}”？`, message: '该会话的消息和临时附件都会被永久删除。', confirmLabel: '确认删除', tone: 'danger' }).then(confirmed => { if (confirmed) deleteMutation.mutate(item.id); }); }} creating={createMutation.isPending} deleting={deleteMutation.isPending ? deleteMutation.variables : undefined}/><main className="conversation-workspace"><header><div><span className="eyebrow">AGENT COLLABORATION</span><h1>{selected ? conversationTitle(selected.title) : 'Agent 协作空间'}</h1></div>{selected && <span>{selected.kind === 'AUTO' ? 'AUTO 默认会话' : `人工会话 #${selected.conversation_no}`}</span>}</header>
      <MessageTimeline conversation={selected} messages={timelineMessages} streamingText={selected ? streamingReplies[selected.id]?.text : undefined} subagents={subagentsQuery.data ?? []} onRetry={id => retryMutation.mutate(id)} onFork={forkFrom} onRevise={(message, text) => reviseMutation.mutate({ message, text })} onPreview={setAttachmentPreview} forkingId={forkMutation.isPending ? forkMutation.variables?.message.id : undefined} revisingId={reviseMutation.isPending ? reviseMutation.variables?.message.id : undefined} retrying={retryMutation.isPending}/>
      {!!queuedMessages.length && <section className="queued-message-stack" aria-label={`排队消息 ${queuedMessages.length} 条`}><header><b>等待当前回合结束</b><span>引导会立即把消息加入正在运行的 Agent 上下文</span></header>{queuedMessages.map((message, index) => <article key={message.id}><span className="queue-position"><CornerDownRight size={15}/><small>{index + 1}</small></span><p title={messageSummary(message)}>{messageSummary(message)}</p><div className="queue-actions"><button type="button" title="立即作为引导发送" disabled={steerMutation.isPending || cancelQueuedMutation.isPending} onClick={() => steerMutation.mutate(message.id)}><CornerDownRight size={14}/>{steerMutation.isPending && steerMutation.variables === message.id ? '引导中…' : '引导'}</button><button type="button" className="queue-remove" aria-label={`移出队列 ${messageSummary(message)}`} title="仅移出队列，不影响当前回合" disabled={steerMutation.isPending || cancelQueuedMutation.isPending} onClick={() => cancelQueuedMutation.mutate(message.id)}><Trash2 size={14}/></button></div></article>)}</section>}
      {selected && (readOnly ? (
        <div className="read-only-composer"><b>历史会话，只读</b><span>验收、退回和重跑请返回运行详情操作。</span><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div>
      ) : (
        <div className="message-composer">
          {!!commandSuggestions.length && <div className="capability-command-menu" role="listbox" aria-label="能力引用候选">{commandSuggestions.map((item, index) => <button type="button" role="option" aria-selected={index === suggestionIndex} className={index === suggestionIndex ? 'active' : ''} key={`${item.capability_type}-${item.capability_key}`} onMouseDown={event => event.preventDefault()} onClick={() => selectCapability(item.capability_type, item.capability_key)}><b>{capabilityMarker(item.capability_type, item.capability_key)}</b><span>{item.capability_type === 'SKILL' ? 'Skill · 调用并遵循能力说明' : 'MCP · 使用 Server 暴露的工具'}</span></button>)}</div>}
          {!!attachments.length && <div className="pending-attachments">{attachments.map(item => <article key={item.id}>{item.preview_url ? <img src={item.preview_url} alt=""/> : <span><FileIcon size={16}/></span>}<div><b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small></div><button type="button" aria-label={`移除附件 ${item.filename}`} onClick={() => removeAttachment(item.id)}><X size={13}/></button></article>)}</div>}
          <textarea aria-label="发送给 Agent 的消息" value={draft} maxLength={20000} placeholder={selected.state === 'CREATING' ? '会话创建中…' : selected.state === 'STOPPING' ? '正在停止 Agent…' : selected.state === 'FAILED' ? '输入新消息并发送，系统会自动重建 Agent 会话。' : replyingToAttempt ? '回复 Agent 并继续执行。输入 $ 引用运行能力。' : '补充要求；输入 $ 引用能力，也可粘贴文件或图片。Enter 发送。'} disabled={selected.state === 'CREATING' || selected.state === 'STOPPING'} onPaste={event => { const files = Array.from(event.clipboardData.files); if (files.length) { event.preventDefault(); void addFiles(files); } }} onChange={event => { setDrafts(old => ({ ...old, [selected.id]: event.target.value })); setSuggestionIndex(0); }} onKeyDown={keyDown}/>
          <input ref={attachmentInputRef} className="attachment-input" type="file" multiple onChange={event => { void addFiles(Array.from(event.target.files ?? [])); event.target.value = ''; }}/>
          <div className="composer-actions">
            <div><button type="button" className="attach-button" aria-label="添加附件" title={replyingToAttempt ? '流程人工回复暂不支持附件，可在人工会话中发送' : '添加图片或文件'} disabled={replyingToAttempt || selected.state === 'STOPPING' || attachments.length >= MAX_ATTACHMENT_COUNT} onClick={() => attachmentInputRef.current?.click()}><Paperclip size={15}/></button><span>{selected.state === 'GENERATING' ? '可停止当前 Agent；本轮草稿会保留' : selected.state === 'STOPPING' ? '正在等待 Runtime 确认停止' : `${draft.length} / 20,000`}</span></div>
            <div className="composer-runtime-actions">
              <RuntimeSelectionMenu models={enabledModels} modelName={runtimeSelection.modelName} reasoningEffort={runtimeSelection.reasoningEffort} disabled={selected.state === 'CREATING' || selected.state === 'STOPPING'} onChange={setRuntimeSelection}/>
              {selected.state === 'GENERATING' ? <button type="button" className="agent-stop-button" aria-label="停止当前 Agent" title="停止当前 Agent" disabled={stopMutation.isPending} onClick={stop}><Square size={11} fill="currentColor"/></button> : <button className="primary" disabled={(!draft.trim() && !attachments.length) || selected.state === 'CREATING' || selected.state === 'STOPPING' || sendMutation.isPending} onClick={send}><Send size={15}/>{replyingToAttempt ? '提交并继续' : selected.state === 'FAILED' ? '重新连接并发送' : '发送'}</button>}
            </div>
          </div>
        </div>
      ))}
      {(attachmentError || createMutation.error || deleteMutation.error || sendMutation.error || retryMutation.error || steerMutation.error || cancelQueuedMutation.error || forkMutation.error || reviseMutation.error || stopMutation.error) && <p className="conversation-error"><AlertTriangle size={14}/>{attachmentError || (createMutation.error || deleteMutation.error || sendMutation.error || retryMutation.error || steerMutation.error || cancelQueuedMutation.error || forkMutation.error || reviseMutation.error || stopMutation.error)?.message}</p>}
    </main><AgentRuntimeSidebar conversation={selected} collapsed={runtimeSidebarCollapsed} onCollapsedChange={setRuntimeSidebarCollapsed}><ContextPanel attempt={attempt} nodeName={nodeName} node={node} runName={runQuery.data.name} messages={messagesQuery.data ?? []} subagents={subagentsQuery.data ?? []} conversation={selected} onPreview={setAttachmentPreview}/></AgentRuntimeSidebar></div>{attachmentPreview && <AttachmentPreviewDialog preview={attachmentPreview} onClose={() => setAttachmentPreview(undefined)}/>}</section>;
}
