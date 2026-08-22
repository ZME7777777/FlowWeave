import { AlertTriangle, ArrowLeft, Bot, CircleDot, Plus, Send, Square, UserRound } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import '../agent-chat.css';
import { api, subscribeToConversationStream } from '../api/client';
import { AgentRuntimeSidebar } from '../components/AgentRuntimeSidebar';
import { RuntimeGovernancePanel } from '../components/RuntimeGovernancePanel';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowRunConversation, OpenHandsConversationEvent } from '../types';

const CONNECTION_LABELS: Record<string, string> = {
  READY: '已连接', RECONNECTING: '重新连接中', DEGRADED: '连接降级', REPLACING: '替换中',
  STARTING: '启动中', NOT_STARTED: '尚未启动', ARCHIVED: '历史归档', STOPPED: '已停止', DELETING: '删除中',
};

function eventRole(event: OpenHandsConversationEvent): 'question' | 'reply' | 'activity' {
  if (event.event_type !== 'MESSAGE') return 'activity';
  const source = String(event.payload.source ?? '').toLowerCase();
  return source === 'user' || source === 'human' ? 'question' : 'reply';
}

function eventTitle(event: OpenHandsConversationEvent): string {
  const role = eventRole(event);
  if (role === 'question') return '提问';
  if (role === 'reply') return '回复';
  return String(event.payload.event_name || event.event_type).replaceAll('_', ' ');
}

function EventTimeline({ events }: { events: OpenHandsConversationEvent[] }) {
  if (!events.length) return <div className="empty conversation-empty"><Bot size={28}/><b>会话已就绪</b><span>发送一个问题开始协作。</span></div>;
  return <div className="conversation-timeline">{events.map(event => {
    const role = eventRole(event);
    const content = typeof event.payload.content === 'string' ? event.payload.content : '';
    return <article key={event.id} className={`message-row ${role === 'question' ? 'human' : role === 'reply' ? 'agent' : 'activity'}`}>
      <span className="message-avatar">{role === 'question' ? <UserRound size={15}/> : <Bot size={15}/>}</span>
      <div className="message-card"><header><b>{eventTitle(event)}</b><code>{event.id.slice(0, 8)}</code></header>{content ? <ReactMarkdown>{content}</ReactMarkdown> : <pre>{JSON.stringify(event.payload.details ?? {}, null, 2)}</pre>}</div>
    </article>;
  })}</div>;
}

function ConversationRail({ conversations, selectedId, disabled, onSelect, onCreate }: {
  conversations: FlowRunConversation[];
  selectedId?: string;
  disabled: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  return <aside className="conversation-rail"><header><div><span className="eyebrow">FLOWRUN CONVERSATIONS</span><b>会话</b></div><button type="button" disabled={disabled} onClick={onCreate} aria-label="新建会话"><Plus size={15}/></button></header><div className="conversation-list">{conversations.map((item, index) => <button key={item.id} className={item.id === selectedId ? 'active' : ''} onClick={() => onSelect(item.id)}><span><b>{item.display_label || `会话 ${index + 1}`}</b><small>最近连接 {new Date(item.last_connected_at).toLocaleString()}</small></span><CircleDot size={12}/></button>)}</div>{!conversations.length && <div className="empty compact">此 FlowRun 尚无会话。</div>}</aside>;
}

export function AgentChatPage() {
  const qc = useQueryClient();
  const { selectedRunId, selectedConversationId, selectConversation, returnToWorkbench } = useWorkbenchStore();
  const [draft, setDraft] = useState('');
  const [streamStatus, setStreamStatus] = useState<'connecting' | 'live' | 'recovering' | 'disabled'>('disabled');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const runQuery = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId) });
  const runtimeQuery = useQuery({ queryKey: ['flow-run-runtime', selectedRunId], queryFn: () => api.runtimeOverview(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 2000 });
  const conversationsQuery = useQuery({ queryKey: ['flow-run-conversations', selectedRunId], queryFn: () => api.conversations(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 4000 });
  const conversations = conversationsQuery.data ?? [];
  const selected = useMemo(() => conversations.find(item => item.id === selectedConversationId) ?? conversations[0], [conversations, selectedConversationId]);
  const selectedId = selected?.id;
  useEffect(() => { if (selectedId && selectedId !== selectedConversationId) selectConversation(selectedId); }, [selectConversation, selectedConversationId, selectedId]);
  const eventsQuery = useQuery({
    queryKey: ['flow-run-conversation-events', selectedRunId, selected?.id],
    queryFn: () => api.conversationEvents(selectedRunId!, selected!.id),
    enabled: Boolean(selectedRunId && selected && runtimeQuery.data?.write_available),
    refetchInterval: 2500,
  });
  const refresh = useCallback(() => {
    if (!selectedRunId) return;
    void qc.invalidateQueries({ queryKey: ['flow-run-runtime', selectedRunId] });
    void qc.invalidateQueries({ queryKey: ['flow-run-conversations', selectedRunId] });
    if (selectedId) void qc.invalidateQueries({ queryKey: ['flow-run-conversation-events', selectedRunId, selectedId] });
  }, [qc, selectedId, selectedRunId]);
  useEffect(() => {
    if (!selectedRunId || !selectedId || !runtimeQuery.data?.write_available) {
      setStreamStatus('disabled');
      return;
    }
    return subscribeToConversationStream(selectedRunId, selectedId, refresh, setStreamStatus);
  }, [refresh, runtimeQuery.data?.write_available, selectedId, selectedRunId]);
  const create = useMutation({
    mutationFn: () => api.createConversation(selectedRunId!, `会话 ${conversations.length + 1}`),
    onSuccess: item => { selectConversation(item.id); refresh(); },
  });
  const send = useMutation({
    mutationFn: () => api.sendConversationQuestion(selectedRunId!, selected!.id, draft.trim()),
    onSuccess: () => { setDraft(''); refresh(); },
  });
  const stop = useMutation({ mutationFn: () => api.stopConversation(selectedRunId!, selected!.id), onSuccess: refresh });
  if (!selectedRunId) return <div className="empty"><b>缺少 FlowRun 上下文</b><button className="secondary" onClick={returnToWorkbench}>返回运行详情</button></div>;
  if (!runQuery.data || !runtimeQuery.data) return <div className="empty">加载 FlowRun 会话…</div>;
  const run = runQuery.data;
  const runtime = runtimeQuery.data;
  const archived = runtime.rerun_required;
  const canCreate = !archived && run.state !== 'COMPLETED' && run.state !== 'CANCELLED';
  const canWrite = Boolean(selected && runtime.write_available && !runtime.read_only);
  return <section className="agent-chat-page"><div className="conversation-topbar"><button onClick={returnToWorkbench}><ArrowLeft size={15}/>返回运行详情</button><div><span>{run.name}</span><b>FlowRun 会话工作台</b></div><span className={`conversation-status ${runtime.connection_state.toLowerCase()}`}>{CONNECTION_LABELS[runtime.connection_state] ?? runtime.connection_state}</span></div>
    <div className={`agent-chat-layout ${sidebarCollapsed ? 'runtime-sidebar-collapsed' : ''}`}><ConversationRail conversations={conversations} selectedId={selected?.id} disabled={!canCreate || create.isPending} onSelect={selectConversation} onCreate={() => create.mutate()}/><main className="conversation-workspace"><header><div><span className="eyebrow">OPENHANDS NATIVE EVENTS</span><h1>{selected?.display_label || '会话、提问与回复'}</h1></div>{runtime.active_generation && <span>generation {runtime.active_generation}</span>}</header>
      {archived ? <div className="read-only-composer"><b>历史运行数据不兼容</b><span>该 Run 没有新的 FlowRun Runtime Session，不能恢复旧平台会话。请从流程定义显式重跑。</span><button className="secondary" onClick={returnToWorkbench}>返回并重跑</button></div> : selected ? <EventTimeline events={eventsQuery.data?.events ?? []}/> : <div className="empty conversation-empty"><Bot size={28}/><b>新建会话</b><span>同一 FlowRun 的所有会话共享一个 Runtime 和 Workspace。</span></div>}
      {selected && !archived && <div className="message-composer"><textarea aria-label="发送问题" value={draft} maxLength={20000} placeholder={runtime.connection_state === 'RECONNECTING' ? 'Runtime 正在重新连接，恢复后可继续提问。' : runtime.connection_state === 'DEGRADED' ? 'Runtime 已降级，请先查看运维诊断。' : '输入问题，Enter 发送；OpenHands user/assistant role 只保留在线路层。'} disabled={!canWrite} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && draft.trim() && canWrite) { event.preventDefault(); send.mutate(); } }}/><div className="composer-actions"><span>{runtime.connection_state === 'READY' ? '消息直接写入 OpenHands 原生事件树' : CONNECTION_LABELS[runtime.connection_state]}</span><div>{canWrite && <button type="button" className="agent-stop-button" aria-label="停止当前 Agent" disabled={stop.isPending} onClick={() => stop.mutate()}><Square size={11} fill="currentColor"/></button>}<button className="primary" disabled={!canWrite || !draft.trim() || send.isPending} onClick={() => send.mutate()}><Send size={15}/>发送问题</button></div></div></div>}
      {(create.error || send.error || stop.error || eventsQuery.error) && <p className="conversation-error"><AlertTriangle size={14}/>{(create.error || send.error || stop.error || eventsQuery.error)?.message}</p>}
    </main><AgentRuntimeSidebar runId={selectedRunId} conversation={selected} runtime={runtime} collapsed={sidebarCollapsed} onCollapsedChange={setSidebarCollapsed} governance={<RuntimeGovernancePanel runId={selectedRunId} runtime={runtime} streamStatus={streamStatus} onRefresh={refresh}/>}><div className="context-panel"><section><span className="eyebrow">FLOWRUN</span><h3>{run.name}</h3><dl><dt>Runtime Session</dt><dd>{runtime.runtime_session_id ? runtime.runtime_session_id.slice(0, 8) : '尚未分配'}</dd><dt>Environment Version</dt><dd>{run.environment_version_id || '历史数据未绑定'}</dd><dt>会话数量</dt><dd>{conversations.length}</dd></dl></section><section><h3>事实边界</h3><p>FlowWeave 只保存 locator、授权与审计；消息、事件树、HEAD、状态和 cursor 均由 OpenHands 持有。</p></section></div></AgentRuntimeSidebar></div></section>;
}
