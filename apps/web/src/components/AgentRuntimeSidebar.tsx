import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { CircleDot, ExternalLink, Info, Maximize2, Minimize2, PanelRightClose, PanelRightOpen, ShieldCheck, Terminal, X } from 'lucide-react';
import { ReactNode, useEffect, useRef, useState } from 'react';
import { agentTerminalUrl } from '../api/client';
import type { AgentConversation } from '../types';
import './AgentRuntimeSidebar.css';

interface Props {
  conversation?: AgentConversation;
  children: ReactNode;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}

interface RuntimeTerminalProps { conversationId: string; standalone?: boolean }

export function RuntimeTerminal({ conversationId, standalone = false }: RuntimeTerminalProps) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在连接当前 Agent 容器…');

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const terminal = new XTerm({
      cursorBlink: true, scrollback: 3000, fontSize: 12, lineHeight: 1.3,
      fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
      theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    terminal.writeln('正在连接当前 Agent 容器…');
    const fitTerminal = () => {
      if (element.clientWidth < 160 || element.clientHeight < 100) return;
      let dimensions: { cols: number; rows: number } | undefined;
      try { dimensions = fit.proposeDimensions(); } catch { return; }
      if (!dimensions || dimensions.cols < 20 || dimensions.rows < 2) return;
      if (terminal.cols !== dimensions.cols || terminal.rows !== dimensions.rows) {
        terminal.resize(dimensions.cols, dimensions.rows);
      }
    };
    fitTerminal();
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let reconnectAttempts = 0;
    const resize = () => {
      fitTerminal();
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', rows: terminal.rows, columns: terminal.cols }));
    };
    const connect = () => {
      if (disposed) return;
      setState('connecting');
      setDetail(reconnectAttempts ? `连接中断，正在自动重连（${reconnectAttempts}/5）…` : '正在连接当前 Agent 容器…');
      const current = new WebSocket(agentTerminalUrl(conversationId, terminal.rows, terminal.cols));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => {
        reconnectAttempts = 0;
        setState('connected');
        setDetail('已连接 · 关闭或切换页面不会终止后台任务');
        terminal.clear();
        resize();
        terminal.focus();
      };
      current.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      current.onerror = () => { /* onclose owns the retry state. */ };
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        reconnectAttempts += 1;
        if (reconnectAttempts <= 5) {
          setState('connecting');
          setDetail(`连接中断，正在自动重连（${reconnectAttempts}/5）…`);
          terminal.writeln(`\r\n[连接中断] ${event.reason || '正在重新连接当前 Agent 容器'}…`);
          reconnectTimer = window.setTimeout(connect, Math.min(1000 * 2 ** (reconnectAttempts - 1), 8000));
          return;
        }
        setState('unavailable');
        setDetail(event.reason || '终端连接失败，请切换标签页后重试');
        terminal.writeln(`\r\n[终端不可用] ${event.reason || '终端连接失败，请稍后重试'}`);
      };
    };
    connect();
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    void document.fonts?.ready.then(resize);
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      observer.disconnect();
      input.dispose();
      socket?.close(1000);
      terminal.dispose();
    };
  }, [conversationId]);

  return <div className={`agent-runtime-terminal ${standalone ? 'standalone' : ''}`}><div className="agent-terminal-status"><i className={state}/><span>{detail}</span></div><div ref={host} className="agent-terminal-screen" aria-label="Agent 运行终端"/></div>;
}

function openStandaloneTerminal(conversationId: string) {
  const url = new URL(window.location.href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('terminalConversation', conversationId);
  window.open(url, `flowweave-terminal-${conversationId}`, 'popup=yes,width=1280,height=820');
}

export function StandaloneAgentTerminal({ conversationId }: { conversationId: string }) {
  return <main className="standalone-terminal-page"><header><div><span className="eyebrow">AGENT RUNTIME TERMINAL</span><h1>Agent 运行终端</h1></div><div><span>终端由后端持续托管，关闭此窗口不会停止命令</span><button type="button" onClick={() => window.close()}><X size={16}/>关闭窗口</button></div></header><RuntimeTerminal conversationId={conversationId} standalone/></main>;
}

export function AgentRuntimeSidebar({ conversation, children, collapsed, onCollapsedChange }: Props) {
  const [tab, setTab] = useState<'context' | 'terminal'>('context');
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setTab('context'), [conversation?.id]);
  useEffect(() => setExpanded(false), [conversation?.id]);
  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [expanded]);
  return <><aside className={`agent-runtime-sidebar ${collapsed ? 'collapsed' : ''}`}><header className="agent-sidebar-header"><div><span className="eyebrow">RUNTIME PANEL</span><b>运行工作台</b></div><nav aria-label="Agent 右侧栏"><button className={tab === 'context' ? 'active' : ''} onClick={() => setTab('context')}><Info size={13}/>上下文</button><button className={tab === 'terminal' ? 'active' : ''} disabled={!conversation} onClick={() => setTab('terminal')}><Terminal size={13}/>终端</button></nav><button type="button" className="agent-sidebar-toggle" onClick={() => onCollapsedChange(!collapsed)} title={collapsed ? '展开运行工作台' : '收起运行工作台'} aria-label={collapsed ? '展开运行工作台' : '收起运行工作台'} aria-expanded={!collapsed}>{collapsed ? <PanelRightOpen size={16}/> : <PanelRightClose size={16}/>}</button></header><div className="agent-runtime-sidebar-body" aria-hidden={collapsed}>{tab === 'context' ? children : conversation ? <div className="agent-terminal-pane"><section className="agent-terminal-summary"><div className="agent-terminal-summary-title"><span><Terminal size={15}/></span><div><b>Agent 运行终端</b><small>{conversation.kind === 'AUTO' ? '默认执行会话' : `人工协作会话 #${conversation.conversation_no}`}</small></div></div><div className="agent-terminal-actions"><button type="button" onClick={() => setExpanded(true)} title="放大终端" aria-label="放大终端"><Maximize2 size={14}/></button><button type="button" onClick={() => openStandaloneTerminal(conversation.id)} title="在独立窗口中打开" aria-label="在独立窗口中打开"><ExternalLink size={14}/></button></div><div className="agent-terminal-facts"><span><CircleDot size={11}/>{conversation.runtime_resource?.lifecycle === 'RUNNING' ? '容器运行中' : conversation.runtime_resource?.lifecycle === 'DELETING' ? '容器删除中' : '容器状态待确认'}</span><span><ShieldCheck size={11}/>{conversation.runtime_resource?.cleanup_policy === 'DELETE_WITH_CONVERSATION' ? '删除会话后回收' : '随 Attempt 回收'}</span>{conversation.runtime_resource && <span className="agent-terminal-resource" title={conversation.runtime_resource.container_name}>Docker <code>{conversation.runtime_resource.container_name}</code></span>}</div></section><RuntimeTerminal conversationId={conversation.id}/></div> : null}</div></aside>{expanded && conversation && <div className="agent-terminal-overlay" role="dialog" aria-modal="true" aria-label="放大的 Agent 运行终端"><section><header><div><Terminal size={18}/><span><b>Agent 运行终端</b><small>关闭放大视图不会终止后台任务</small></span></div><div><button type="button" onClick={() => openStandaloneTerminal(conversation.id)}><ExternalLink size={15}/>新窗口</button><button type="button" onClick={() => setExpanded(false)}><Minimize2 size={15}/>退出放大</button></div></header><RuntimeTerminal conversationId={conversation.id}/></section></div>}</>;
}
