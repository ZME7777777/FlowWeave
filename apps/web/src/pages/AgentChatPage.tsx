import { AlertTriangle, ArrowLeft, Bot, CheckCircle2, ChevronRight, Clock3, CornerDownRight, Download, File as FileIcon, FileText, GitFork, Image as ImageIcon, Link2, LoaderCircle, Paperclip, Pencil, Plus, RefreshCw, Send, Sparkles, Square, Trash2, Workflow, X, XCircle } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyboardEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import '../agent-chat.css';
import { api, messageAttachmentUrl, randomId, subscribeToRun, workspaceImageUrl } from '../api/client';
import { AgentRuntimeSidebar } from '../components/AgentRuntimeSidebar';
import { useProductDialog } from '../components/ProductDialogContext';
import { useWorkbenchStore } from '../store/workbench';
import type { AgentConversation, AgentMessage, AgentMessageAttachmentPart, MessageAttachmentInput, NodeAttempt } from '../types';

const MAX_ATTACHMENT_COUNT = 4;
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024;
interface PendingAttachment extends MessageAttachmentInput { id: string; preview_url?: string }

const STATE_LABELS: Record<string, string> = {
  CREATING: '创建中', IDLE: '在线', GENERATING: 'Agent 生成中',
  STOPPING: '正在停止',
  WAITING_HUMAN: '等待你的回复', FAILED: '连接失败', READ_ONLY: '历史会话',
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

function MarkdownMessage({ text, messageId }: { text: string; messageId?: string }) {
  return <div className="message-markdown"><ReactMarkdown
    urlTransform={value => isWorkspaceImage(value) ? value : defaultUrlTransform(value)}
    components={{ img: ({ src, alt }) => <img src={messageId && src && isWorkspaceImage(src) ? workspaceImageUrl(messageId, src) : src} alt={alt ?? ''} loading="lazy"/> }}
  >{text}</ReactMarkdown></div>;
}

function MessageAttachmentList({ message }: { message: AgentMessage }) {
  const attachments = messageAttachments(message);
  if (!attachments.length) return null;
  return <div className="message-attachments">{attachments.map(item => {
    const url = messageAttachmentUrl(message.id, item.attachment_id);
    return <a key={item.attachment_id} href={messageAttachmentUrl(message.id, item.attachment_id, true)} className={item.mime_type.startsWith('image/') ? 'image' : ''} title={`下载 ${item.filename}`}>
      {item.mime_type.startsWith('image/') ? <img src={url} alt={item.filename}/> : <span><FileIcon size={16}/></span>}
      <b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small><Download size={13}/>
    </a>;
  })}</div>;
}

function eventDetails(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function usefulDetails(value: unknown): Record<string, unknown> {
  return Object.fromEntries(Object.entries(eventDetails(value)).filter(([, item]) => item !== '' && item !== null && item !== undefined));
}

function eventName(payload: Record<string, unknown>, fallback: string) {
  const raw = String(payload.event_name || fallback).replace(/(Action|Observation|Event)$/g, '');
  return raw || fallback;
}

function ActivityMessage({ message }: { message: AgentMessage }) {
  if (message.message_type === 'STATE') {
    const state = eventDetails(message.content.state);
    const text = typeof state.content === 'string' ? state.content.trim() : '';
    if (!text) return null;
    return <details className="activity-message thought-message"><summary><span><Sparkles size={15}/><b>思考过程</b><small>{text.split('\n')[0]}</small></span><ChevronRight size={14}/></summary><MarkdownMessage text={text} messageId={message.id}/></details>;
  }
  const tool = eventDetails(message.content.tool);
  const details = usefulDetails(tool.details);
  const content = typeof tool.content === 'string' ? tool.content.trim() : '';
  const fallback = message.message_type === 'TOOL_RESULT' ? '工具结果' : '工具调用';
  const name = eventName(tool, fallback);
  if (!content && !Object.keys(details).length && !tool.event_name) return null;
  const completed = message.message_type === 'TOOL_RESULT';
  return <details className={`activity-message tool-activity ${completed ? 'completed' : 'running'}`}><summary><span>{completed ? <CheckCircle2 size={15}/> : <Sparkles size={15}/>}<b>{completed ? '工具执行结果' : '调用工具'} · {name}</b><small>{content.split('\n')[0] || (Object.keys(details).length ? `${Object.keys(details).length} 项参数` : '')}</small></span><ChevronRight size={14}/></summary><div className="activity-detail">{content && <MarkdownMessage text={content} messageId={message.id}/>} {!!Object.keys(details).length && <pre>{JSON.stringify(details, null, 2)}</pre>}</div></details>;
}

const isActivityMessage = (message: AgentMessage) => message.message_type === 'TOOL_CALL' || message.message_type === 'TOOL_RESULT' || message.message_type === 'STATE';

function activitySummary(message: AgentMessage) {
  if (message.message_type === 'STATE') {
    const state = eventDetails(message.content.state);
    const text = typeof state.content === 'string' ? state.content.trim().split('\n')[0] : '';
    return text ? `正在思考 · ${text}` : '正在分析上下文';
  }
  const tool = eventDetails(message.content.tool);
  const fallback = message.message_type === 'TOOL_RESULT' ? '工具结果' : '工具调用';
  return message.message_type === 'TOOL_RESULT'
    ? `${eventName(tool, fallback)} 已返回，等待 Agent 下一步`
    : `正在调用 ${eventName(tool, fallback)}`;
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
  const duration = formatDuration(
    startedAt ?? messages[0].created_at,
    running ? clock : completedAt ?? messages[messages.length - 1].created_at,
  );
  return <details className={`activity-group ${running ? 'running' : 'completed'}`} open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><span>{running ? <LoaderCircle size={14}/> : <CheckCircle2 size={14}/>}<b>{running ? '处理中' : '已处理'}</b><small>{duration}</small>{running && <em>{activitySummary(last)}</em>}</span><ChevronRight size={14}/></summary>
    <div className="activity-group-content">{messages.map(message => <ActivityMessage key={message.id} message={message}/>)}</div>
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

function MessageBubble({ message, onRetry, onFork, onEdit, final, conversationState, latestHuman, forking }: {
  message: AgentMessage;
  onRetry: (id: string) => void;
  onFork: (message: AgentMessage) => void;
  onEdit: (message: AgentMessage) => void;
  final: boolean;
  conversationState?: AgentConversation['state'];
  latestHuman: boolean;
  forking: boolean;
}) {
  const meta = message.source === 'PROGRAM'
    ? { label: '流程自动发送', className: 'program' }
    : message.source === 'HUMAN'
      ? { label: '人工接管', className: 'human' }
      : { label: '模型回复', className: 'agent' };
  if (message.message_type === 'TOOL_CALL' || message.message_type === 'TOOL_RESULT' || message.message_type === 'STATE') return <ActivityMessage message={message}/>;
  if (message.message_type === 'ERROR') {
    return <article className="message-error"><XCircle size={17}/><div><b>执行遇到问题</b><p>{messageText(message)}</p></div></article>;
  }
  return <article className={`chat-message ${meta.className} ${final ? 'final-answer' : ''}`}>
    <div className="message-avatar" aria-label={meta.label} title={meta.label}><SourceIcon source={message.source}/></div>
    <div className="message-body"><header>{message.source !== 'HUMAN' && <b>{final ? '最终答复' : meta.label}</b>}<span>{new Date(message.created_at).toLocaleTimeString()}</span></header>{!!message.content.capability_refs?.length && <div className="message-capability-refs">{message.content.capability_refs.map(item => <span key={`${item.capability_type}-${item.capability_key}`}>{item.capability_type === 'SKILL' ? 'Skill' : 'MCP'} · {item.capability_key}</span>)}</div>}{messageText(message) && (message.source === 'AGENT' ? <MarkdownMessage text={messageText(message)} messageId={message.id}/> : <p>{messageText(message)}</p>)}<MessageAttachmentList message={message}/>
      {message.message_type === 'TEXT' && message.source !== 'PROGRAM' && messageText(message) && <div className="message-branch-actions">{message.source === 'HUMAN' && <button type="button" disabled={forking} onClick={() => onEdit(message)} title="保留原消息，并从此处修改上下文创建新会话"><Pencil size={12}/>编辑并分支</button>}<button type="button" disabled={forking} onClick={() => onFork(message)} title="复制此消息及之前的历史到新会话"><GitFork size={12}/>{forking ? '正在创建…' : '从此分叉'}</button></div>}
      {message.source !== 'AGENT' && <footer className={`delivery ${message.delivery_state.toLowerCase()}`}>{deliveryLabel(message, conversationState, latestHuman)}{message.delivery_state === 'FAILED' && <>{message.error_detail && <span>{message.error_code === 'RUNTIME_CONVERSATION_MISSING' || message.error_code === 'EXECUTOR_UNAVAILABLE' ? 'Agent 连接已失效，重试会自动重建' : message.error_detail}</span>}<button onClick={() => onRetry(message.id)}><RefreshCw size={12}/>重试并重连</button></>}</footer>}
    </div>
  </article>;
}

function MessageTimeline({ conversation, messages, onRetry, onFork, onEdit, forkingId }: {
  conversation?: AgentConversation;
  messages: AgentMessage[];
  onRetry: (id: string) => void;
  onFork: (message: AgentMessage) => void;
  onEdit: (message: AgentMessage) => void;
  forkingId?: string;
}) {
  const timelineRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const timelineItems = groupTimelineMessages(messages);
  const lastActivityGroupId = [...timelineItems].reverse().find(item => item.type === 'activity')?.id;
  const latestInputId = [...messages].reverse().find(message => message.source === 'HUMAN' || message.source === 'PROGRAM')?.id;
  const version = messages.map(message => `${message.id}:${message.delivery_state}`).join('|');
  const scrollToBottom = useCallback(() => {
    const timeline = timelineRef.current;
    if (timeline) timeline.scrollTop = timeline.scrollHeight;
  }, []);

  useLayoutEffect(() => {
    followRef.current = true;
    scrollToBottom();
  }, [conversation?.id, scrollToBottom]);
  useLayoutEffect(() => {
    if (followRef.current) scrollToBottom();
  }, [conversation?.state, scrollToBottom, version]);
  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (followRef.current) scrollToBottom();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [conversation?.id, scrollToBottom]);

  return <div
    ref={timelineRef}
    className="message-timeline"
    aria-live="polite"
    aria-label="对话消息"
    onScroll={event => {
      const timeline = event.currentTarget;
      followRef.current = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= 80;
    }}
  ><div ref={contentRef} className="message-timeline-content">
    {timelineItems.map(item => {
      if (item.type === 'activity') {
        const firstSequence = item.messages[0].sequence_no;
        const lastSequence = item.messages[item.messages.length - 1].sequence_no;
        const startedBy = [...messages].reverse().find(message => message.sequence_no < firstSequence && (message.source === 'HUMAN' || message.source === 'PROGRAM'));
        const completedBy = messages.find(message => message.sequence_no > lastSequence && message.source === 'AGENT' && message.message_type === 'TEXT');
        const running = conversation?.state === 'GENERATING' && item.id === lastActivityGroupId && !completedBy;
        return <ActivityGroup key={item.id} messages={item.messages} running={running} startedAt={startedBy?.created_at} completedAt={completedBy?.created_at ?? item.messages[item.messages.length - 1].created_at}/>;
      }
      const message = item.message;
      return <MessageBubble key={item.id} message={message} onRetry={onRetry} onFork={onFork} onEdit={onEdit} forking={message.id === forkingId} final={message.content.presentation === 'final'} conversationState={conversation?.state} latestHuman={message.id === latestInputId}/>;
    })}
    {conversation && !messages.length && <div className="conversation-empty"><Bot size={26}/><b>当前轮次上下文已挂载</b><span>发送第一条消息开始协作。</span></div>}
    {!conversation && <div className="conversation-empty"><Workflow size={26}/><b>尚无可用会话</b><span>本轮开始执行后将自动创建默认会话。</span></div>}
  </div></div>;
}

function AgentActivityStatus({ conversation, messages, retrying, onRetry }: { conversation: AgentConversation; messages: AgentMessage[]; retrying: boolean; onRetry: (id: string) => void }) {
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    if (conversation.state !== 'GENERATING') return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [conversation.state]);
  const activeMessages = messages.filter(message => message.delivery_state !== 'CANCELLED');
  const latestHuman = [...activeMessages].reverse().find(message => message.source === 'HUMAN' || message.source === 'PROGRAM');
  const waitingDelivery = [...activeMessages].reverse().find(message => message.delivery_state === 'QUEUED' && message.content.presentation !== 'queued');
  const delivering = [...activeMessages].reverse().find(message => message.delivery_state === 'DELIVERING');
  const turnMessages = activeMessages.filter(message => !latestHuman || message.sequence_no > latestHuman.sequence_no);
  const activityMessages = turnMessages.filter(message => isActivityMessage(message));
  const latestActivity = activityMessages[activityMessages.length - 1];
  const thoughtCount = activityMessages.filter(message => message.message_type === 'STATE').length;
  const quietSeconds = latestActivity
    ? Math.max(0, Math.round((clock - new Date(latestActivity.created_at).getTime()) / 1000))
    : 0;
  const quietLabel = quietSeconds >= 60
    ? `${Math.floor(quietSeconds / 60)} 分 ${quietSeconds % 60} 秒`
    : `${quietSeconds} 秒`;
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
    const thoughtDetail = thoughtCount
      ? `本轮已收到 ${thoughtCount} 条可展示的思考事件。`
      : '本轮尚未收到可展示的思考事件。';
    if (!latestActivity) {
      title = '等待 Agent 活动事件';
      detail = `消息已送达，但尚未收到模型或工具事件。${thoughtDetail}`;
    } else if (latestActivity.message_type === 'TOOL_CALL') {
      const tool = eventDetails(latestActivity.content.tool);
      title = `工具调用中 · ${eventName(tool, '工具')}`;
      detail = `最后事件距今 ${quietLabel}。${thoughtDetail}`;
    } else if (latestActivity.message_type === 'TOOL_RESULT') {
      const tool = eventDetails(latestActivity.content.tool);
      title = quietSeconds >= 30 ? 'Agent 事件流长时间无更新' : '工具结果已返回';
      detail = `${eventName(tool, '工具')} 已返回，之后 ${quietLabel} 没有新事件。${thoughtDetail}`;
      if (quietSeconds >= 30) { tone = 'waiting'; icon = <Clock3 size={15}/>; }
    } else {
      title = '已收到模型思考事件';
      detail = `最后一条思考事件距今 ${quietLabel}；正在等待下一条模型或工具事件。`;
    }
  } else if (conversation.state === 'WAITING_HUMAN') {
    tone = 'waiting';
    title = 'Agent 正在等待你的回复';
    detail = '补充信息后，本轮执行会从当前上下文继续。';
    icon = <Clock3 size={15}/>;
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

function ContextPanel({ attempt, nodeName, node, runName, messages, conversationKind }: { attempt: NodeAttempt; nodeName: string; node?: { asset: { executor: { model_name?: string | null } | null; workspace_ref?: string; capabilities: Array<{ capability_type: string; capability_key: string }> } }; runName: string; messages: AgentMessage[]; conversationKind?: AgentConversation['kind'] }) {
  const attachments = messages.flatMap(message => messageAttachments(message).map(item => ({ message, item })));
  const urls = [...new Set(messages.filter(message => message.source === 'HUMAN').flatMap(message => messageText(message).match(/https?:\/\/[^\s<>"']+/g) ?? []).map(value => value.replace(/[()[\]{},.;:!?，。；：！？]+$/, '')))];
  const collaboration = conversationKind === 'HUMAN_CREATED';
  return <aside className="conversation-context"><header><span className="eyebrow">EXECUTION CONTEXT</span><h2>执行上下文</h2></header><section><h3>当前事实</h3><dl><dt>流程运行</dt><dd>{runName}</dd><dt>节点</dt><dd>{nodeName}</dd><dt>执行轮次</dt><dd>第 {attempt.attempt_no} 轮</dd><dt>状态 / 版本</dt><dd>{attempt.state} · v{attempt.state_version}</dd></dl></section><section><h3>模型与能力</h3><dl><dt>模型</dt><dd>{node?.asset.executor?.model_name || '服务默认'}</dd><dt>能力策略</dt><dd>{collaboration ? '按消息动态选择' : '按执行轮次选择'}</dd><dt>宿主工作区</dt><dd>{node?.asset.workspace_ref ? `var/workspaces/${node.asset.workspace_ref}` : '—'}</dd></dl>{node?.asset.capabilities.map(item => <span className="context-capability" key={`${item.capability_type}-${item.capability_key}`}>{item.capability_type} · {item.capability_key}</span>)}</section><section><h3>会话来源</h3>{attachments.map(({ message, item }) => <a className="conversation-source" key={`${message.id}-${item.attachment_id}`} href={messageAttachmentUrl(message.id, item.attachment_id, true)}><span>{item.mime_type.startsWith('image/') ? <ImageIcon size={14}/> : <FileIcon size={14}/>}</span><b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small><Download size={12}/></a>)}{urls.map(url => <a className="conversation-source url" key={url} href={url} target="_blank" rel="noreferrer"><span><Link2 size={14}/></span><b>{url}</b><small>URL</small></a>)}{!attachments.length && !urls.length && <p className="context-empty">消息中的附件和 URL 会集中显示在这里。</p>}</section><section><h3>输入与本轮产物</h3>{attempt.input_bindings.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.input_field_key}</span>)}{attempt.artifacts.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.field_key} · v{item.version_no}<small>{item.artifact_type}</small></span>)}</section><footer>{collaboration ? '人工会话共享本轮事实与能力，但不继承其他会话的启动任务。' : '这些会话只属于当前执行轮次；退回后产生的新轮次会建立独立上下文并保留这里的历史。'}</footer></aside>;
}

export function AgentChatPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId, selectConversation, returnToWorkbench } = useWorkbenchStore();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pendingAttachments, setPendingAttachments] = useState<Record<string, PendingAttachment[]>>({});
  const [attachmentError, setAttachmentError] = useState('');
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [runtimeSidebarCollapsed, setRuntimeSidebarCollapsed] = useState(false);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const runQuery = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId) });
  const attempt = useMemo(() => runQuery.data?.node_runs.find(item => item.id === selectedNodeRunId)?.attempts.find(item => item.id === selectedAttemptId), [runQuery.data, selectedAttemptId, selectedNodeRunId]);
  const conversationsQuery = useQuery({ queryKey: ['attempt-conversations', selectedAttemptId], queryFn: () => api.conversations(selectedAttemptId!), enabled: Boolean(selectedAttemptId), refetchInterval: 2500 });
  const conversations = conversationsQuery.data ?? [];
  const selected = conversations.find(item => item.id === selectedConversationId) ?? conversations[0];
  const messagesQuery = useQuery({ queryKey: ['conversation-messages', selected?.id], queryFn: () => api.conversationMessages(selected!.id), enabled: Boolean(selected), refetchInterval: selected?.state === 'GENERATING' || selected?.state === 'CREATING' || selected?.state === 'STOPPING' ? 1500 : 4000 });
  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['attempt-conversations', selectedAttemptId] });
    if (selected?.id) void qc.invalidateQueries({ queryKey: ['conversation-messages', selected.id] });
    if (selectedRunId) void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] });
  }, [qc, selected?.id, selectedAttemptId, selectedRunId]);
  useEffect(() => selectedRunId ? subscribeToRun(selectedRunId, refresh) : undefined, [refresh, selectedRunId]);
  useEffect(() => { if (selected && selected.id !== selectedConversationId) selectConversation(selected.id); }, [selectConversation, selected, selectedConversationId]);
  const createMutation = useMutation({ mutationFn: () => api.createConversation(attempt!.id, attempt!.state_version), onSuccess: item => { selectConversation(item.id); refresh(); } });
  const deleteMutation = useMutation({ mutationFn: api.deleteConversation, onSuccess: (_, deletedId) => { const next = conversations.find(item => item.id !== deletedId); qc.removeQueries({ queryKey: ['conversation-messages', deletedId] }); if (next) selectConversation(next.id); refresh(); } });
  const sendMutation = useMutation({
    mutationFn: async ({ conversation, content, capabilityRefs, attachments }: { conversation: AgentConversation; content: string; capabilityRefs: Array<{ capability_type: 'SKILL' | 'MCP'; capability_key: string }>; attachments: PendingAttachment[] }): Promise<AgentMessage | undefined> => {
      if (conversation.kind === 'AUTO' && attempt!.state === 'WAITING_HUMAN') {
        if (attachments.length) throw new Error('流程等待人工回复时暂不能附加文件，请先在人工会话中发送附件。');
        await api.humanInput(attempt!.id, content, attempt!.state_version);
        return undefined;
      }
      return api.sendConversationMessage(conversation.id, content, conversation.state_version, capabilityRefs, attachments);
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
    mutationFn: ({ message, editedText }: { message: AgentMessage; editedText?: string }) => {
      if (!selected) throw new Error('当前没有可分叉的会话。');
      return api.forkConversationMessage(message.id, selected.state_version, editedText);
    },
    onSuccess: conversation => {
      qc.setQueryData<AgentConversation[]>(['attempt-conversations', selectedAttemptId], current => current?.some(item => item.id === conversation.id) ? current : [...(current ?? []), conversation]);
      selectConversation(conversation.id);
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
  const send = () => { const content = draft.trim(); if (selected && (content || attachments.length) && !readOnly) { const capabilityRefs = callableCapabilities.filter(item => selectedCapabilityKeys.includes(`${item.capability_type}:${item.capability_key}`)).map(item => ({ capability_type: item.capability_type as 'SKILL' | 'MCP', capability_key: item.capability_key })); sendMutation.mutate({ conversation: selected, content, capabilityRefs, attachments }); } };
  const stop = () => {
    if (!selected || selected.state !== 'GENERATING') return;
    const automatic = selected.kind === 'AUTO';
    void dialog.confirm({
      title: automatic ? '停止当前节点执行？' : '强制终止当前 Agent 回合？',
      message: automatic
        ? '自动会话与当前节点执行共享 Runtime。停止后当前执行轮次会被取消，流程中的其他记录仍会保留。'
        : '当前 Agent 回合会被立即中断；消息和会话上下文仍会保留，之后可以继续发送新消息。',
      confirmLabel: '强制终止',
      tone: 'danger',
    }).then(confirmed => { if (confirmed) stopMutation.mutate(selected); });
  };
  const forkFrom = (message: AgentMessage) => {
    void dialog.confirm({
      title: '从此消息创建新会话？',
      message: '该消息及之前的历史会被复制到独立会话；原会话不会改变。',
      confirmLabel: '创建分支',
    }).then(confirmed => { if (confirmed) forkMutation.mutate({ message }); });
  };
  const editFrom = (message: AgentMessage) => {
    void dialog.prompt({
      title: '编辑消息并创建分支',
      message: '原消息和原会话保持不变；新会话会继承该消息之前的历史，并以修改后的内容继续。',
      inputLabel: '修改后的消息',
      initialValue: messageText(message),
      confirmLabel: '创建并发送',
    }).then(value => { if (value?.trim()) forkMutation.mutate({ message, editedText: value.trim() }); });
  };
  const selectCapability = (type: string, key: string) => { if (!selected) return; const marker = capabilityMarker(type, key); let next = draft; if (commandMatch) { const markerStart = (commandMatch.index ?? 0) + commandMatch[1].length; next = `${draft.slice(0, markerStart)}${marker} `; } else if (!new RegExp(`(^|\\s)${escapeRegExp(marker)}(?=\\s|$)`).test(draft)) { next = `${draft}${draft && !draft.endsWith(' ') ? ' ' : ''}${marker} `; } setDrafts(old => ({ ...old, [selected.id]: next })); setSuggestionIndex(0); };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (commandSuggestions.length) { if (event.key === 'ArrowDown') { event.preventDefault(); setSuggestionIndex(index => (index + 1) % commandSuggestions.length); return; } if (event.key === 'ArrowUp') { event.preventDefault(); setSuggestionIndex(index => (index - 1 + commandSuggestions.length) % commandSuggestions.length); return; } if (event.key === 'Escape') { event.preventDefault(); setSuggestionIndex(0); return; } if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); const item = commandSuggestions[Math.min(suggestionIndex, commandSuggestions.length - 1)]; selectCapability(item.capability_type, item.capability_key); return; } } if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } };
  return <section className="agent-chat-page"><div className="conversation-topbar"><button onClick={returnToWorkbench}><ArrowLeft size={15}/>返回运行详情</button><div><span>{runQuery.data.name}</span><b>{nodeName} · 第 {attempt.attempt_no} 轮</b></div><span className={`conversation-status ${selected?.state.toLowerCase() ?? ''}`}>{selected ? STATE_LABELS[selected.state] : '等待会话'}</span></div>
    <div className={`agent-chat-layout ${runtimeSidebarCollapsed ? 'runtime-sidebar-collapsed' : ''}`}><ConversationRail conversations={conversations} selectedId={selected?.id} attempt={attempt} onSelect={selectConversation} onCreate={() => createMutation.mutate()} onDelete={item => { void dialog.confirm({ title: `删除“${conversationTitle(item.title)}”？`, message: '该会话的消息和临时附件都会被永久删除。', confirmLabel: '确认删除', tone: 'danger' }).then(confirmed => { if (confirmed) deleteMutation.mutate(item.id); }); }} creating={createMutation.isPending} deleting={deleteMutation.isPending ? deleteMutation.variables : undefined}/><main className="conversation-workspace"><header><div><span className="eyebrow">AGENT COLLABORATION</span><h1>{selected ? conversationTitle(selected.title) : 'Agent 协作空间'}</h1></div>{selected && <span>{selected.kind === 'AUTO' ? 'AUTO 默认会话' : `人工会话 #${selected.conversation_no}`}</span>}</header>
      <MessageTimeline conversation={selected} messages={timelineMessages} onRetry={id => retryMutation.mutate(id)} onFork={forkFrom} onEdit={editFrom} forkingId={forkMutation.isPending ? forkMutation.variables?.message.id : undefined}/>
      {selected && <AgentActivityStatus conversation={selected} messages={messagesQuery.data ?? []} retrying={retryMutation.isPending} onRetry={id => retryMutation.mutate(id)}/>}
      {!!queuedMessages.length && <section className="queued-message-stack" aria-label={`排队消息 ${queuedMessages.length} 条`}><header><b>等待当前回合结束</b><span>引导会立即把消息加入正在运行的 Agent 上下文</span></header>{queuedMessages.map((message, index) => <article key={message.id}><span className="queue-position"><CornerDownRight size={15}/><small>{index + 1}</small></span><p title={messageSummary(message)}>{messageSummary(message)}</p><div className="queue-actions"><button type="button" title="立即作为引导发送" disabled={steerMutation.isPending || cancelQueuedMutation.isPending} onClick={() => steerMutation.mutate(message.id)}><CornerDownRight size={14}/>{steerMutation.isPending && steerMutation.variables === message.id ? '引导中…' : '引导'}</button><button type="button" className="queue-remove" aria-label={`移出队列 ${messageSummary(message)}`} title="仅移出队列，不影响当前回合" disabled={steerMutation.isPending || cancelQueuedMutation.isPending} onClick={() => cancelQueuedMutation.mutate(message.id)}><Trash2 size={14}/></button></div></article>)}</section>}
      {selected && (readOnly ? <div className="read-only-composer"><b>历史会话，只读</b><span>验收、退回和重跑请返回运行详情操作。</span><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div> : <div className="message-composer">{!!commandSuggestions.length && <div className="capability-command-menu" role="listbox" aria-label="能力引用候选">{commandSuggestions.map((item, index) => <button type="button" role="option" aria-selected={index === suggestionIndex} className={index === suggestionIndex ? 'active' : ''} key={`${item.capability_type}-${item.capability_key}`} onMouseDown={event => event.preventDefault()} onClick={() => selectCapability(item.capability_type, item.capability_key)}><b>{capabilityMarker(item.capability_type, item.capability_key)}</b><span>{item.capability_type === 'SKILL' ? 'Skill · 调用并遵循能力说明' : 'MCP · 使用 Server 暴露的工具'}</span></button>)}</div>}{!!attachments.length && <div className="pending-attachments">{attachments.map(item => <article key={item.id}>{item.preview_url ? <img src={item.preview_url} alt=""/> : <span><FileIcon size={16}/></span>}<div><b>{item.filename}</b><small>{formatBytes(item.byte_size)}</small></div><button type="button" aria-label={`移除附件 ${item.filename}`} onClick={() => removeAttachment(item.id)}><X size={13}/></button></article>)}</div>}<textarea aria-label="发送给 Agent 的消息" value={draft} maxLength={20000} placeholder={selected.state === 'CREATING' ? '会话创建中…' : selected.state === 'STOPPING' ? '正在停止 Agent…' : selected.state === 'FAILED' ? '输入新消息并发送，系统会自动重建 Agent 会话。' : replyingToAttempt ? '回复 Agent 并继续执行。输入 $ 引用运行能力。' : '补充要求；输入 $ 引用能力，也可粘贴文件或图片。Enter 发送。'} disabled={selected.state === 'CREATING' || selected.state === 'STOPPING'} onPaste={event => { const files = Array.from(event.clipboardData.files); if (files.length) { event.preventDefault(); void addFiles(files); } }} onChange={event => { setDrafts(old => ({ ...old, [selected.id]: event.target.value })); setSuggestionIndex(0); }} onKeyDown={keyDown}/><input ref={attachmentInputRef} className="attachment-input" type="file" multiple onChange={event => { void addFiles(Array.from(event.target.files ?? [])); event.target.value = ''; }}/><div className="composer-actions"><div><button type="button" className="attach-button" aria-label="添加附件" title={replyingToAttempt ? '流程人工回复暂不支持附件，可在人工会话中发送' : '添加图片或文件'} disabled={replyingToAttempt || selected.state === 'STOPPING' || attachments.length >= MAX_ATTACHMENT_COUNT} onClick={() => attachmentInputRef.current?.click()}><Paperclip size={15}/></button><span>{selected.state === 'GENERATING' ? '可停止当前 Agent；本轮草稿会保留' : selected.state === 'STOPPING' ? '正在等待 Runtime 确认停止' : `${draft.length} / 20,000`}</span></div>{selected.state === 'GENERATING' ? <button type="button" className="agent-stop-button" aria-label="停止当前 Agent" title="停止当前 Agent" disabled={stopMutation.isPending} onClick={stop}><Square size={11} fill="currentColor"/></button> : <button className="primary" disabled={(!draft.trim() && !attachments.length) || selected.state === 'CREATING' || selected.state === 'STOPPING' || sendMutation.isPending} onClick={send}><Send size={15}/>{replyingToAttempt ? '提交并继续' : selected.state === 'FAILED' ? '重新连接并发送' : '发送'}</button>}</div></div>)}
      {(attachmentError || createMutation.error || deleteMutation.error || sendMutation.error || retryMutation.error || steerMutation.error || cancelQueuedMutation.error || forkMutation.error || stopMutation.error) && <p className="conversation-error"><AlertTriangle size={14}/>{attachmentError || (createMutation.error || deleteMutation.error || sendMutation.error || retryMutation.error || steerMutation.error || cancelQueuedMutation.error || forkMutation.error || stopMutation.error)?.message}</p>}
    </main><AgentRuntimeSidebar conversation={selected} collapsed={runtimeSidebarCollapsed} onCollapsedChange={setRuntimeSidebarCollapsed}><ContextPanel attempt={attempt} nodeName={nodeName} node={node} runName={runQuery.data.name} messages={messagesQuery.data ?? []} conversationKind={selected?.kind}/></AgentRuntimeSidebar></div></section>;
}
