import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, ChevronRight, CircleDot, LoaderCircle, PanelRightOpen, Pencil, Plus, Send, Settings2, Square, Terminal, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, agentWorkspaceTerminalUrl, api, subscribeToAgentWorkspaceStream } from '../api/client';
import { ConversationSurface } from '../components/ConversationSurface';
import type { AgentConversation } from '../types';
import './agent-workbench.css';
import './agent-workbench-layout.css';

type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';

function bindingIdFromLocation(): string | undefined {
  const match = window.location.pathname.match(/^\/agent\/conversations\/([^/]+)$/);
  return match ? decodeURIComponent(match[1]) : undefined;
}

function conversationName(conversation: AgentConversation, index: number) {
  return conversation.display_title || `未命名会话 ${index + 1}`;
}

function WorkspaceTerminal({ workspaceId }: { workspaceId: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在连接工作区终端…');

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const terminal = new XTerm({ cursorBlink: true, scrollback: 3000, fontSize: 12, lineHeight: 1.3, fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, monospace", theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' } });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    let socket: WebSocket | null = null;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let attempts = 0;
    const reconnectDelays = [1000, 2000, 5000, 10_000, 30_000];
    const resize = () => {
      if (element.clientWidth < 160 || element.clientHeight < 100) return;
      try { fit.fit(); } catch { return; }
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', rows: terminal.rows, columns: terminal.cols }));
    };
    const connect = () => {
      if (disposed) return;
      setState('connecting');
      setDetail(attempts ? '终端已断开，正在重新连接…' : '正在连接工作区终端…');
      const current = new WebSocket(agentWorkspaceTerminalUrl(workspaceId, terminal.rows, terminal.cols));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => { attempts = 0; setState('connected'); setDetail('已连接共享工作区'); resize(); terminal.focus(); };
      current.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        if (event.code === 4409 || attempts >= reconnectDelays.length) { setState('unavailable'); setDetail(event.reason || '终端暂时不可用'); return; }
        const delay = reconnectDelays[attempts];
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    connect();
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => { disposed = true; if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); observer.disconnect(); input.dispose(); socket?.close(1000); terminal.dispose(); };
  }, [workspaceId]);

  return <section className="agent-workspace-terminal"><header><span className={`terminal-dot ${state}`}/><span>{detail}</span></header><div ref={host} aria-label="Agent 工作区终端"/></section>;
}

interface Props { onNavigate: (path: string, replace?: boolean) => void; onOpenModels: () => void; }

export function AgentWorkbenchPage({ onNavigate, onOpenModels }: Props) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('disabled');
  const [liveText, setLiveText] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [selectedProvider, setSelectedProvider] = useState('');
  const selectedBindingId = bindingIdFromLocation();
  const workspaceQuery = useQuery({ queryKey: ['agent-workspace-default'], queryFn: api.defaultAgentWorkspace, retry: false });
  const workspace = workspaceQuery.data;
  const runtimeQuery = useQuery({ queryKey: ['agent-workspace-runtime', workspace?.id], queryFn: () => api.agentWorkspaceRuntime(workspace!.id), enabled: Boolean(workspace), refetchInterval: result => result.state.data?.state === 'RECOVERING' ? 5000 : false });
  const conversationsQuery = useQuery({ queryKey: ['agent-conversations', workspace?.id], queryFn: () => api.agentConversations(workspace!.id), enabled: Boolean(workspace) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers, enabled: Boolean(workspace) });
  const conversations = useMemo(() => conversationsQuery.data ?? [], [conversationsQuery.data]);
  const selected = useMemo(() => conversations.find(item => item.id === selectedBindingId), [conversations, selectedBindingId]);
  const runtime = runtimeQuery.data;
  const canWrite = Boolean(workspace && runtime?.write_available && workspace.default_model_provider_id);
  const eventsQuery = useQuery({
    queryKey: ['agent-conversation-events', workspace?.id, selected?.id], queryFn: () => api.agentConversationEvents(workspace!.id, selected!.id), enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 1200 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const refresh = useCallback(() => {
    if (!workspace) return;
    void queryClient.invalidateQueries({ queryKey: ['agent-workspace-runtime', workspace.id] });
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    if (selected?.id) void queryClient.invalidateQueries({ queryKey: ['agent-conversation-events', workspace.id, selected.id] });
  }, [queryClient, selected?.id, workspace]);
  const onStreamEvent = useCallback((event: { type: 'delta' | 'message_complete'; content?: string }) => {
    if (event.type === 'delta' && event.content) setLiveText(value => value + event.content);
    if (event.type === 'message_complete') { setLiveText(''); setIsGenerating(false); refresh(); }
  }, [refresh]);

  useEffect(() => { if (!selectedBindingId && conversations.length) onNavigate(`/agent/conversations/${encodeURIComponent(conversations[0].id)}`, true); if (selectedBindingId && conversations.length && !selected) onNavigate('/agent', true); }, [conversations, onNavigate, selected, selectedBindingId]);
  useEffect(() => { if (!workspace || !selected || !runtime?.write_available) { setStreamStatus('disabled'); return; } return subscribeToAgentWorkspaceStream(workspace.id, selected.id, onStreamEvent, setStreamStatus); }, [onStreamEvent, runtime?.write_available, selected, workspace]);
  useEffect(() => { setEditing(false); setTitle(selected?.display_title ?? ''); setLiveText(''); setIsGenerating(false); }, [selected?.id, selected?.display_title]);
  useEffect(() => { setSelectedProvider(workspace?.default_model_provider_id ?? ''); }, [workspace?.default_model_provider_id]);

  const create = useMutation({ mutationFn: () => api.createAgentConversation(workspace!.id), onSuccess: value => { void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace?.id] }); onNavigate(`/agent/conversations/${encodeURIComponent(value.id)}`); } });
  const saveSettings = useMutation({ mutationFn: () => api.updateAgentWorkspaceSettings(workspace!.id, selectedProvider || null), onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['agent-workspace-default'] }); } });
  const rename = useMutation({ mutationFn: () => api.updateAgentConversation(workspace!.id, selected!.id, title.trim()), onSuccess: () => { setEditing(false); refresh(); } });
  const remove = useMutation({ mutationFn: () => api.deleteAgentConversation(workspace!.id, selected!.id), onSuccess: () => { setDrawerOpen(false); onNavigate('/agent', true); refresh(); } });
  const send = useMutation({ mutationFn: () => api.sendAgentMessage(workspace!.id, selected!.id, draft.trim()), onSuccess: () => { setDraft(''); setLiveText(''); setIsGenerating(true); refresh(); }, onError: () => setIsGenerating(false) });
  const interrupt = useMutation({ mutationFn: () => api.interruptAgentConversation(workspace!.id, selected!.id), onSuccess: () => { setIsGenerating(false); refresh(); } });

  if (workspaceQuery.isLoading) return <main className="agent-workbench-loading">正在打开 Agent 工作台…</main>;
  if (workspaceQuery.error || !workspace) return <main className="agent-workbench-loading"><b>Agent 工作台正在初始化</b><span>默认运行环境准备完成后，会话列表会自动出现。</span></main>;
  const defaultMissing = !workspace.default_model_provider_id;
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const selectedProviderChanged = selectedProvider !== (workspace.default_model_provider_id ?? '');
  const providerLabel = (item: typeof connectedProviders[number]) => `${item.name} · ${item.models.find(model => model.enabled && model.is_default)?.model_name}`;

  return <main className="agent-workbench-page">
    <aside className="agent-workbench-rail">
      <header><div><span className="eyebrow">AGENT WORKSPACE</span><h1>Agent 会话</h1></div><button className="primary" disabled={!canWrite || create.isPending} onClick={() => create.mutate()}><Plus size={15}/>{create.isPending ? '创建中…' : '新建会话'}</button></header>
      <div className="agent-workbench-list">{conversations.map((item, index) => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => onNavigate(`/agent/conversations/${encodeURIComponent(item.id)}`)}><CircleDot size={13}/><span><b>{conversationName(item, index)}</b><small>{item.lifecycle === 'ACTIVE' ? '可继续会话' : '正在处理'}</small></span><ChevronRight size={13}/></button>)}</div>
      {!conversations.length && <div className="agent-workbench-rail-empty"><Bot size={25}/><b>还没有会话</b><span>{defaultMissing ? '先完成模型配置，再创建会话。' : '新建一个会话开始协作。'}</span></div>}
      <section className="agent-workbench-model-card"><header><Settings2 size={14}/><b>新会话模型配置</b></header>{connectedProviders.length ? <><select aria-label="Agent 新会话模型配置" value={selectedProvider} disabled={saveSettings.isPending} onChange={event => setSelectedProvider(event.target.value)}><option value="">暂不选择</option>{connectedProviders.map(item => <option key={item.id} value={item.id}>{providerLabel(item)}</option>)}</select><button type="button" className="secondary" disabled={!selectedProviderChanged || saveSettings.isPending} onClick={() => saveSettings.mutate()}>{saveSettings.isPending ? '保存中…' : selectedProvider ? '保存配置' : '清空配置'}</button></> : <button className="secondary" onClick={onOpenModels}>前往模型配置</button>}<small>仅影响后续新会话</small></section>
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div><span className="eyebrow">DIRECT AGENT SESSION</span>{editing ? <div className="agent-title-edit"><input aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) rename.mutate(); if (event.key === 'Escape') setEditing(false); }}/><button className="primary" disabled={!title.trim() || rename.isPending} onClick={() => rename.mutate()}>保存</button></div> : <h2>{selected ? conversationName(selected, conversations.indexOf(selected)) : '开始一个新的会话'}</h2>}</div><div className="agent-header-actions">{selected && <><button type="button" aria-label="重命名会话" onClick={() => setEditing(true)}><Pencil size={14}/></button><button type="button" className="danger" aria-label="删除会话" disabled={remove.isPending} onClick={() => remove.mutate()}><Trash2 size={14}/></button></>}<button type="button" aria-label={drawerOpen ? '关闭工作区抽屉' : '打开工作区终端'} className={drawerOpen ? 'active' : ''} disabled={!runtime?.write_available} onClick={() => setDrawerOpen(value => !value)}><Terminal size={15}/></button></div></header>
      {defaultMissing ? <section className="agent-model-onboarding"><Settings2 size={20}/><div><b>先选择已测试成功的模型配置</b><p>默认容器已在后台运行；请在左侧选择并保存新会话模型配置，然后即可直接新建 Agent 会话。</p></div></section> : runtime?.state === 'RECOVERING' ? <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '会话列表和标题已保留，恢复后可继续使用。'}</span></div></section> : selected ? <ConversationSurface events={eventsQuery.data?.events ?? []} liveText={liveText} isGenerating={isGenerating}/> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>每个会话共享同一工作区，但保留独立的对话与事件记录。</span><button className="primary" disabled={!canWrite || create.isPending} onClick={() => create.mutate()}><Plus size={15}/>新建会话</button></div>}
      {selected && !defaultMissing && runtime?.state !== 'RECOVERING' && <div className="agent-composer"><textarea aria-label="发送 Agent 消息" value={draft} maxLength={200_000} placeholder="输入消息，Enter 发送，Shift+Enter 换行" disabled={!canWrite} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && draft.trim() && canWrite) { event.preventDefault(); send.mutate(); } }}/><footer><span>{streamStatus === 'live' ? isGenerating ? 'Agent 正在实时回复' : '已实时连接' : streamStatus === 'recovering' ? '连接恢复中' : '消息由 OpenHands 处理'}</span><div>{canWrite && <button className="agent-interrupt" aria-label="停止当前 Agent" disabled={interrupt.isPending || !isGenerating} onClick={() => interrupt.mutate()}><Square size={11} fill="currentColor"/></button>}<button className="primary" disabled={!canWrite || !draft.trim() || send.isPending} onClick={() => send.mutate()}><Send size={15}/>{send.isPending ? '发送中…' : '发送'}</button></div></footer></div>}
      {(create.error || saveSettings.error || rename.error || remove.error || send.error || interrupt.error || eventsQuery.error) && <p className="agent-workbench-error">{(create.error || saveSettings.error || rename.error || remove.error || send.error || interrupt.error || eventsQuery.error)?.message}</p>}
    </section>
    <aside className={`agent-workspace-drawer ${drawerOpen ? 'open' : ''}`}><header><div><span className="eyebrow">WORKSPACE</span><b>共享工作区终端</b></div><button type="button" aria-label="关闭终端抽屉" onClick={() => setDrawerOpen(false)}><X size={16}/></button></header>{drawerOpen ? <WorkspaceTerminal workspaceId={workspace.id}/> : <div className="agent-drawer-empty"><PanelRightOpen size={20}/><span>打开终端即可查看和操作 Agent 使用的共享工作区。</span></div>}</aside>
  </main>;
}
