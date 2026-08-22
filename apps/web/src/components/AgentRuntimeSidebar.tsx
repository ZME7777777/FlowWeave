import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { ExternalLink, Info, Maximize2, Minimize2, PanelRightClose, PanelRightOpen, Radar, Terminal, X } from 'lucide-react';
import { ReactNode, useEffect, useRef, useState } from 'react';
import { agentTerminalUrl } from '../api/client';
import type { FlowRunConversation, FlowRunRuntimeOverview } from '../types';
import './AgentRuntimeSidebar.css';

interface Props {
  runId: string;
  conversation?: FlowRunConversation;
  runtime?: FlowRunRuntimeOverview;
  children: ReactNode;
  governance?: ReactNode;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}

interface RuntimeTerminalProps { runId: string; conversationId: string; standalone?: boolean }

export function RuntimeTerminal({ runId, conversationId, standalone = false }: RuntimeTerminalProps) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在通过 FlowWeave 授权代理连接 Runtime…');

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
    let socket: WebSocket | null = null;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let attempts = 0;
    const resize = () => {
      if (element.clientWidth < 160 || element.clientHeight < 100) return;
      try { fit.fit(); } catch { return; }
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', rows: terminal.rows, columns: terminal.cols }));
    };
    const connect = () => {
      if (disposed) return;
      setState('connecting');
      setDetail(attempts ? '连接中断，正在重新解析 active generation…' : '正在通过 FlowWeave 授权代理连接 Runtime…');
      const current = new WebSocket(agentTerminalUrl(runId, conversationId, terminal.rows, terminal.cols));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => { attempts = 0; setState('connected'); setDetail('已连接 active generation'); resize(); terminal.focus(); };
      current.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        attempts += 1;
        if (attempts <= 5 && event.code !== 4409) {
          reconnectTimer = window.setTimeout(connect, Math.min(1000 * 2 ** (attempts - 1), 8000));
          return;
        }
        setState('unavailable');
        setDetail(event.reason || 'Runtime 当前不可连接');
      };
    };
    connect();
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      observer.disconnect();
      input.dispose();
      socket?.close(1000);
      terminal.dispose();
    };
  }, [conversationId, runId]);

  return <div className={`agent-runtime-terminal ${standalone ? 'standalone' : ''}`}><div className="agent-terminal-status"><i className={state}/><span>{detail}</span></div><div ref={host} className="agent-terminal-screen" aria-label="Agent 运行终端"/></div>;
}

function openStandaloneTerminal(runId: string, conversationId: string) {
  const url = new URL(window.location.href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('terminalRun', runId);
  url.searchParams.set('terminalConversation', conversationId);
  window.open(url, `flowweave-terminal-${conversationId}`, 'popup=yes,width=1280,height=820');
}

export function StandaloneAgentTerminal({ runId, conversationId }: { runId: string; conversationId: string }) {
  return <main className="standalone-terminal-page"><header><div><span className="eyebrow">FLOWRUN RUNTIME TERMINAL</span><h1>Agent 运行终端</h1></div><div><span>连接始终经 FlowWeave 重新解析 active generation</span><button type="button" onClick={() => window.close()}><X size={16}/>关闭窗口</button></div></header><RuntimeTerminal runId={runId} conversationId={conversationId} standalone/></main>;
}

export function AgentRuntimeSidebar({ runId, conversation, runtime, children, governance, collapsed, onCollapsedChange }: Props) {
  const [tab, setTab] = useState<'context' | 'governance' | 'terminal'>('context');
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setTab('context'), [conversation?.id]);
  const terminal = conversation && runtime?.write_available ? <div className="agent-terminal-pane"><section className="agent-terminal-summary"><div className="agent-terminal-summary-title"><span><Terminal size={15}/></span><div><b>FlowRun Runtime 终端</b><small>active generation {runtime.active_generation ?? '—'}</small></div></div><div className="agent-terminal-actions"><button type="button" onClick={() => setExpanded(true)} aria-label="放大终端"><Maximize2 size={14}/></button><button type="button" onClick={() => openStandaloneTerminal(runId, conversation.id)} aria-label="在独立窗口中打开"><ExternalLink size={14}/></button></div></section><RuntimeTerminal runId={runId} conversationId={conversation.id}/></div> : <div className="empty compact">Runtime 当前不可写，终端已关闭。</div>;
  return <><aside className={`agent-runtime-sidebar ${collapsed ? 'collapsed' : ''}`}><header className="agent-sidebar-header"><div><span className="eyebrow">RUNTIME PANEL</span><b>运行工作台</b></div><nav aria-label="Agent 右侧栏"><button className={tab === 'context' ? 'active' : ''} onClick={() => setTab('context')}><Info size={13}/>上下文</button><button className={tab === 'governance' ? 'active' : ''} disabled={!governance} onClick={() => setTab('governance')}><Radar size={13}/>运维</button><button className={tab === 'terminal' ? 'active' : ''} disabled={!conversation} onClick={() => setTab('terminal')}><Terminal size={13}/>终端</button></nav><button type="button" className="agent-sidebar-toggle" onClick={() => onCollapsedChange(!collapsed)} aria-label={collapsed ? '展开运行工作台' : '收起运行工作台'}>{collapsed ? <PanelRightOpen size={16}/> : <PanelRightClose size={16}/>}</button></header><div className="agent-runtime-sidebar-body" aria-hidden={collapsed}>{tab === 'context' ? children : tab === 'governance' ? governance : terminal}</div></aside>{expanded && conversation && <div className="agent-terminal-overlay" role="dialog" aria-modal="true"><section><header><div><Terminal size={18}/><span><b>FlowRun Runtime 终端</b><small>物理 endpoint 不会暴露给客户端</small></span></div><div><button type="button" onClick={() => openStandaloneTerminal(runId, conversation.id)}><ExternalLink size={15}/>新窗口</button><button type="button" onClick={() => setExpanded(false)}><Minimize2 size={15}/>退出放大</button></div></header><RuntimeTerminal runId={runId} conversationId={conversation.id}/></section></div>}</>;
}
