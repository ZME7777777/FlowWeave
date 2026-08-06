import { AlertTriangle, ArrowLeft, Bot, Clock3, FileText, Plus, RefreshCw, Send, Sparkles, Workflow } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyboardEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { api, subscribeToRun } from '../api/client';
import { useWorkbenchStore } from '../store/workbench';
import type { AgentConversation, AgentMessage, NodeAttempt } from '../types';

const STATE_LABELS: Record<string, string> = {
  CREATING: '创建中', IDLE: '在线', GENERATING: 'Agent 生成中',
  WAITING_HUMAN: '等待你的回复', FAILED: '连接失败', READ_ONLY: '历史会话',
};
const DELIVERY_LABELS: Record<string, string> = {
  QUEUED: '排队中', DELIVERING: '发送中', DELIVERED: '已发送',
  FAILED: '发送失败', CANCELLED: '已取消',
};
const STARTED_STATES = new Set(['EXECUTING', 'WAITING_HUMAN', 'END_GATES', 'END_BLOCKED', 'WAITING_ACCEPTANCE']);

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
  if (message.content.parts?.length) return message.content.parts.map(part => part.text).join('\n');
  if (message.content.tool) return JSON.stringify(message.content.tool, null, 2);
  if (message.content.error) return JSON.stringify(message.content.error, null, 2);
  return JSON.stringify(message.content, null, 2);
}

function MessageBubble({ message, onRetry }: { message: AgentMessage; onRetry: (id: string) => void }) {
  const meta = message.source === 'PROGRAM'
    ? { label: '流程自动发送', className: 'program' }
    : message.source === 'HUMAN'
      ? { label: '人工接管', className: 'human' }
      : { label: '模型回复', className: 'agent' };
  if (message.message_type === 'TOOL_CALL' || message.message_type === 'TOOL_RESULT') {
    return <article className="tool-message"><Sparkles size={15}/><div><b>能力调用 · {message.message_type === 'TOOL_CALL' ? '执行中' : '已完成'}</b><pre>{messageText(message)}</pre></div></article>;
  }
  return <article className={`chat-message ${meta.className}`}>
    <div className="message-avatar" aria-label={meta.label} title={meta.label}><SourceIcon source={message.source}/></div>
    <div className="message-body"><header>{message.source !== 'HUMAN' && <b>{meta.label}</b>}<span>{new Date(message.created_at).toLocaleTimeString()}</span></header><p>{messageText(message)}</p>
      {message.source !== 'AGENT' && <footer className={`delivery ${message.delivery_state.toLowerCase()}`}>{DELIVERY_LABELS[message.delivery_state] ?? message.delivery_state}{message.delivery_mode === 'QUEUE_AFTER_TURN' && message.delivery_state === 'QUEUED' ? ' · 当前回合结束后发送' : ''}{message.delivery_state === 'FAILED' && <button onClick={() => onRetry(message.id)}><RefreshCw size={12}/>重试</button>}</footer>}
    </div>
  </article>;
}

function ConversationRail({ conversations, selectedId, attempt, onSelect, onCreate, creating }: {
  conversations: AgentConversation[]; selectedId?: string; attempt: NodeAttempt;
  onSelect: (id: string) => void; onCreate: () => void; creating: boolean;
}) {
  const canCreate = STARTED_STATES.has(attempt.state);
  return <aside className="conversation-rail"><header><div><span className="eyebrow">CONVERSATIONS</span><h2>协作会话</h2></div><button className="secondary icon-button" aria-label="新建会话" title={canCreate ? '新建会话' : 'Attempt 开始执行后可新建'} disabled={!canCreate || creating} onClick={onCreate}><Plus size={16}/></button></header>
    <div className="conversation-list">{conversations.map(item => <button key={item.id} className={item.id === selectedId ? 'active' : ''} onClick={() => onSelect(item.id)}><span className={`conversation-kind ${item.kind === 'AUTO' ? 'auto' : 'human'}`}>{item.kind === 'AUTO' ? <Workflow size={13}/> : <HumanControlIcon size={14}/>}</span><span><b>{item.title}</b><small>{STATE_LABELS[item.state] ?? item.state} · {item.message_count} 条消息</small></span>{item.state === 'WAITING_HUMAN' && <i/>}</button>)}</div>
    {!conversations.length && <div className="conversation-empty"><Clock3 size={22}/><b>暂无会话</b><span>{canCreate ? '新建人工会话开始协作。' : 'Attempt 开始执行后会自动建立默认会话。'}</span></div>}
  </aside>;
}

function ContextPanel({ attempt, nodeName, node, runName }: { attempt: NodeAttempt; nodeName: string; node?: { asset: { executor: { model_name?: string | null } | null; default_skill_ref?: string | null; capabilities: Array<{ capability_type: string; capability_key: string }> } }; runName: string }) {
  return <aside className="conversation-context"><header><span className="eyebrow">EXECUTION CONTEXT</span><h2>执行上下文</h2></header><section><h3>当前事实</h3><dl><dt>流程运行</dt><dd>{runName}</dd><dt>节点</dt><dd>{nodeName}</dd><dt>Attempt</dt><dd>#{attempt.attempt_no}</dd><dt>状态 / 版本</dt><dd>{attempt.state} · v{attempt.state_version}</dd></dl></section><section><h3>模型与能力</h3><dl><dt>模型</dt><dd>{node?.asset.executor?.model_name || '服务默认'}</dd><dt>默认 Skill</dt><dd>{node?.asset.default_skill_ref || '—'}</dd></dl>{node?.asset.capabilities.map(item => <span className="context-capability" key={`${item.capability_type}-${item.capability_key}`}>{item.capability_type} · {item.capability_key}</span>)}</section><section><h3>输入与候选产物</h3>{attempt.input_bindings.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.input_field_key}<small>{item.binding_source}</small></span>)}{attempt.artifacts.map(item => <span className="context-reference" key={item.id}><FileText size={13}/>{item.field_key} · v{item.version_no}<small>{item.artifact_type}</small></span>)}</section><footer>程序与人工消息均以 <code>user</code> role 发送；来源只用于展示和审计。system prompt 不在消息流中展示。</footer></aside>;
}

export function AgentChatPage() {
  const qc = useQueryClient();
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId, selectConversation, returnToWorkbench } = useWorkbenchStore();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const runQuery = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId) });
  const attempt = useMemo(() => runQuery.data?.node_runs.find(item => item.id === selectedNodeRunId)?.attempts.find(item => item.id === selectedAttemptId), [runQuery.data, selectedAttemptId, selectedNodeRunId]);
  const conversationsQuery = useQuery({ queryKey: ['attempt-conversations', selectedAttemptId], queryFn: () => api.conversations(selectedAttemptId!), enabled: Boolean(selectedAttemptId), refetchInterval: 2500 });
  const conversations = conversationsQuery.data ?? [];
  const selected = conversations.find(item => item.id === selectedConversationId) ?? conversations[0];
  const messagesQuery = useQuery({ queryKey: ['conversation-messages', selected?.id], queryFn: () => api.conversationMessages(selected!.id), enabled: Boolean(selected), refetchInterval: selected?.state === 'GENERATING' || selected?.state === 'CREATING' ? 1500 : 4000 });
  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['attempt-conversations', selectedAttemptId] });
    if (selected?.id) void qc.invalidateQueries({ queryKey: ['conversation-messages', selected.id] });
    if (selectedRunId) void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] });
  }, [qc, selected?.id, selectedAttemptId, selectedRunId]);
  useEffect(() => selectedRunId ? subscribeToRun(selectedRunId, refresh) : undefined, [refresh, selectedRunId]);
  useEffect(() => { if (selected && selected.id !== selectedConversationId) selectConversation(selected.id); }, [selectConversation, selected, selectedConversationId]);
  const createMutation = useMutation({ mutationFn: () => api.createConversation(attempt!.id, attempt!.state_version), onSuccess: item => { selectConversation(item.id); refresh(); } });
  const sendMutation = useMutation({
    mutationFn: async ({ conversation, content }: { conversation: AgentConversation; content: string }): Promise<void> => {
      if (conversation.kind === 'AUTO' && attempt!.state === 'WAITING_HUMAN') {
        await api.humanInput(attempt!.id, content, attempt!.state_version);
      } else {
        await api.sendConversationMessage(conversation.id, content, conversation.state_version);
      }
    },
    onSuccess: (_, variables) => { setDrafts(old => ({ ...old, [variables.conversation.id]: '' })); refresh(); },
  });
  const retryMutation = useMutation({ mutationFn: api.retryConversationMessage, onSuccess: refresh });
  if (!selectedRunId || !selectedNodeRunId || !selectedAttemptId) return <div className="empty"><b>缺少运行上下文</b><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div>;
  if (!runQuery.data || !attempt) return <div className="empty">加载 Agent 协作空间…</div>;
  const nodeRun = runQuery.data.node_runs.find(item => item.id === selectedNodeRunId)!;
  const snapshot = runQuery.data.snapshots.find(item => item.id === attempt.snapshot_id);
  const node = snapshot?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  const nodeName = node?.alias || node?.asset.name || nodeRun.flow_node_snapshot_key;
  const draft = selected ? drafts[selected.id] ?? '' : '';
  const readOnly = selected?.state === 'READ_ONLY';
  const replyingToAttempt = selected?.kind === 'AUTO' && attempt.state === 'WAITING_HUMAN';
  const send = () => { const content = draft.trim(); if (selected && content && !readOnly) sendMutation.mutate({ conversation: selected, content }); };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } };
  return <section className="agent-chat-page"><div className="conversation-topbar"><button onClick={returnToWorkbench}><ArrowLeft size={15}/>返回运行详情</button><div><span>{runQuery.data.name}</span><b>{nodeName} · Attempt {attempt.attempt_no}</b></div><span className={`conversation-status ${selected?.state.toLowerCase() ?? ''}`}>{selected ? STATE_LABELS[selected.state] : '等待会话'}</span></div>
    <div className="agent-chat-layout"><ConversationRail conversations={conversations} selectedId={selected?.id} attempt={attempt} onSelect={selectConversation} onCreate={() => createMutation.mutate()} creating={createMutation.isPending}/><main className="conversation-workspace"><header><div><span className="eyebrow">AGENT COLLABORATION</span><h1>{selected?.title ?? 'Agent 协作空间'}</h1></div>{selected && <span>{selected.kind === 'AUTO' ? 'AUTO 默认会话' : `人工会话 #${selected.conversation_no}`}</span>}</header>
      <div className="message-timeline" aria-live="polite">{messagesQuery.data?.map(message => <MessageBubble key={message.id} message={message} onRetry={id => retryMutation.mutate(id)}/>)}{selected && !messagesQuery.data?.length && <div className="conversation-empty"><Bot size={26}/><b>当前 Attempt 上下文已挂载</b><span>发送第一条消息开始协作。</span></div>}{!selected && <div className="conversation-empty"><Workflow size={26}/><b>尚无可用会话</b><span>执行开始后将自动创建默认会话。</span></div>}</div>
      {selected && (readOnly ? <div className="read-only-composer"><b>历史会话，只读</b><span>验收、退回和重跑请返回运行详情操作。</span><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div> : <div className="message-composer"><textarea aria-label="发送给 Agent 的消息" value={draft} maxLength={20000} placeholder={selected.state === 'CREATING' ? '会话创建中…' : replyingToAttempt ? '回复 Agent 并继续执行。Enter 发送，Shift+Enter 换行。' : '补充约束、纠正方向或回复 Agent。Enter 发送，Shift+Enter 换行。'} disabled={selected.state === 'CREATING' || selected.state === 'FAILED'} onChange={event => setDrafts(old => ({ ...old, [selected.id]: event.target.value }))} onKeyDown={keyDown}/><div><span>{selected.state === 'GENERATING' ? '将在当前回合结束后发送' : `${draft.length} / 20,000`}</span><button className="primary" disabled={!draft.trim() || selected.state === 'CREATING' || selected.state === 'FAILED' || sendMutation.isPending} onClick={send}><Send size={15}/>{replyingToAttempt ? '提交并继续' : selected.state === 'GENERATING' ? '加入队列' : '发送'}</button></div></div>)}
      {(createMutation.error || sendMutation.error || retryMutation.error) && <p className="conversation-error"><AlertTriangle size={14}/>{(createMutation.error || sendMutation.error || retryMutation.error)?.message}</p>}
    </main><ContextPanel attempt={attempt} nodeName={nodeName} node={node} runName={runQuery.data.name}/></div></section>;
}
