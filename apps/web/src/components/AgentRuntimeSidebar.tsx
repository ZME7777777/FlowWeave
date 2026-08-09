import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { CircleDot, ExternalLink, Info, Maximize2, Minimize2, ShieldCheck, Terminal, X } from 'lucide-react';
import { ReactNode, useEffect, useRef, useState } from 'react';
import { agentTerminalUrl } from '../api/client';
import type { AgentConversation } from '../types';
import './AgentRuntimeSidebar.css';

interface Props { conversation?: AgentConversation; children: ReactNode }

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
    const socket = new WebSocket(agentTerminalUrl(conversationId));
    socket.binaryType = 'arraybuffer';
    const resize = () => {
      try { fit.fit(); } catch { return; }
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', rows: terminal.rows, columns: terminal.cols }));
    };
    socket.onopen = () => { setState('connected'); setDetail('已连接 · 关闭或切换页面不会终止后台任务'); terminal.clear(); resize(); terminal.focus(); };
    socket.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
    socket.onclose = event => {
      if (event.code === 1000) return;
      setState('unavailable');
      setDetail(event.reason || '当前会话未绑定可交互的终端镜像');
      terminal.writeln(`\r\n[终端不可用] ${event.reason || '当前会话没有运行容器'}`);
    };
    const input = terminal.onData(data => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => { observer.disconnect(); input.dispose(); socket.close(1000); terminal.dispose(); };
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

export function AgentRuntimeSidebar({ conversation, children }: Props) {
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
  return <><aside className="agent-runtime-sidebar"><header className="agent-sidebar-header"><div><span className="eyebrow">RUNTIME PANEL</span><b>运行工作台</b></div><nav aria-label="Agent 右侧栏"><button className={tab === 'context' ? 'active' : ''} onClick={() => setTab('context')}><Info size={13}/>上下文</button><button className={tab === 'terminal' ? 'active' : ''} disabled={!conversation} onClick={() => setTab('terminal')}><Terminal size={13}/>终端</button></nav></header><div className="agent-runtime-sidebar-body">{tab === 'context' ? children : conversation ? <div className="agent-terminal-pane"><section className="agent-terminal-summary"><div className="agent-terminal-summary-title"><span><Terminal size={15}/></span><div><b>Agent 运行终端</b><small>{conversation.kind === 'AUTO' ? '默认执行会话' : `人工协作会话 #${conversation.conversation_no}`}</small></div></div><div className="agent-terminal-actions"><button type="button" onClick={() => setExpanded(true)} title="放大终端" aria-label="放大终端"><Maximize2 size={14}/></button><button type="button" onClick={() => openStandaloneTerminal(conversation.id)} title="在独立窗口中打开" aria-label="在独立窗口中打开"><ExternalLink size={14}/></button></div><div className="agent-terminal-facts"><span><CircleDot size={11}/>同一运行容器</span><span><ShieldCheck size={11}/>离开页面继续运行</span></div></section><RuntimeTerminal conversationId={conversation.id}/></div> : null}</div></aside>{expanded && conversation && <div className="agent-terminal-overlay" role="dialog" aria-modal="true" aria-label="放大的 Agent 运行终端"><section><header><div><Terminal size={18}/><span><b>Agent 运行终端</b><small>关闭放大视图不会终止后台任务</small></span></div><div><button type="button" onClick={() => openStandaloneTerminal(conversation.id)}><ExternalLink size={15}/>新窗口</button><button type="button" onClick={() => setExpanded(false)}><Minimize2 size={15}/>退出放大</button></div></header><RuntimeTerminal conversationId={conversation.id}/></section></div>}</>;
}
