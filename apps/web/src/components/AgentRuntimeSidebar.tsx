import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { ExternalLink, Info, Maximize2, Minimize2, PanelRightClose, PanelRightOpen, Radar, Terminal, X } from 'lucide-react';
import { createPortal } from 'react-dom';
import { ReactNode, useEffect, useRef, useState } from 'react';
import { agentTerminalUrl, flowRunTerminalUrl } from '../api/client';
import type { FlowRunConversation, FlowRunRuntimeOverview } from '../types';
import { useEscapeClose } from './useEscapeClose';
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

interface RuntimeTerminalProps { runId: string; conversationId?: string; standalone?: boolean }

type RuntimeTerminalContextMenu = { x: number; y: number; text: string; line: string };

async function copyTerminalText(value: string): Promise<void> {
  if (!value) return;
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Keep the terminal usable in an embedded browser without Clipboard API
      // permission, matching the product's other copy affordances.
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

export function RuntimeTerminal({ runId, conversationId, standalone = false }: RuntimeTerminalProps) {
  const host = useRef<HTMLDivElement>(null);
  const sendTerminalInput = useRef<(data: string) => void>(() => undefined);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在通过 FlowWeave 授权代理连接 Runtime…');
  const [contextMenu, setContextMenu] = useState<RuntimeTerminalContextMenu>();

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    setContextMenu(undefined);
    const terminal = new XTerm({
      cursorBlink: true, scrollback: 3000, fontSize: 12, lineHeight: 1.3,
      fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
      theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    const document = element.ownerDocument;
    let removeForcedSelectionListeners: (() => void) | undefined;
    const terminalCellForMouseEvent = (event: MouseEvent) => {
      const screen = element.querySelector<HTMLElement>('.xterm-screen');
      if (!screen) return undefined;
      const bounds = screen.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return undefined;
      const column = Math.max(0, Math.min(terminal.cols - 1, Math.floor((event.clientX - bounds.left) * terminal.cols / bounds.width)));
      const viewportRow = Math.max(0, Math.min(terminal.rows - 1, Math.floor((event.clientY - bounds.top) * terminal.rows / bounds.height)));
      return { column, row: terminal.buffer.active.viewportY + viewportRow };
    };
    const forceTextSelection = (event: MouseEvent) => {
      if (event.button !== 0 || event.shiftKey || terminal.modes.mouseTrackingMode === 'none') return;
      const start = terminalCellForMouseEvent(event);
      if (!start) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      let selecting = false;
      const updateSelection = (current: MouseEvent) => {
        const end = terminalCellForMouseEvent(current);
        if (!end) return;
        const startOffset = start.row * terminal.cols + start.column;
        const endOffset = end.row * terminal.cols + end.column;
        const first = startOffset <= endOffset ? start : end;
        terminal.select(first.column, first.row, Math.abs(endOffset - startOffset));
      };
      const removeListeners = () => {
        document.removeEventListener('mousemove', moveSelection, true);
        document.removeEventListener('mouseup', finishSelection, true);
        removeForcedSelectionListeners = undefined;
      };
      const moveSelection = (current: MouseEvent) => {
        current.preventDefault();
        current.stopImmediatePropagation();
        if (!selecting && Math.hypot(current.clientX - event.clientX, current.clientY - event.clientY) > 2) selecting = true;
        if (selecting) updateSelection(current);
      };
      const finishSelection = (current: MouseEvent) => {
        current.preventDefault();
        current.stopImmediatePropagation();
        if (selecting) updateSelection(current);
        else terminal.focus();
        removeListeners();
      };
      removeForcedSelectionListeners?.();
      removeForcedSelectionListeners = removeListeners;
      document.addEventListener('mousemove', moveSelection, true);
      document.addEventListener('mouseup', finishSelection, true);
    };
    const terminalScreen = element.querySelector<HTMLElement>('.xterm-screen');
    terminalScreen?.addEventListener('mousedown', forceTextSelection, { capture: true });
    const terminalTextAt = (event: MouseEvent) => {
      const cell = terminalCellForMouseEvent(event);
      const line = cell ? terminal.buffer.active.getLine(cell.row)?.translateToString(true) ?? '' : '';
      const selected = terminal.getSelection().trim();
      if (selected) return { text: selected, line };
      const index = cell ? Math.min(Math.max(cell.column, 0), Math.max(0, line.length - 1)) : 0;
      const before = line.slice(0, index + 1).match(/[^\s]+$/)?.[0] ?? '';
      const after = line.slice(index + 1).match(/^[^\s]+/)?.[0] ?? '';
      return { text: before + after, line };
    };
    const openTerminalContextMenu = (event: MouseEvent) => {
      if (event.button !== 2 || !element.contains(event.target as Node)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const { text, line } = terminalTextAt(event);
      setContextMenu({ x: Math.min(event.clientX, window.innerWidth - 230), y: Math.min(event.clientY, window.innerHeight - 250), text, line });
      terminal.focus();
    };
    const closeTerminalContextMenu = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : undefined;
      if (event.button !== 2 && !target?.closest('.agent-terminal-context-menu')) setContextMenu(undefined);
    };
    const suppressTerminalRightMouseUp = (event: MouseEvent) => {
      if (event.button === 2 && element.contains(event.target as Node)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    const suppressBrowserContextMenu = (event: MouseEvent) => {
      if (!element.contains(event.target as Node)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const dismissTerminalContextMenu = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setContextMenu(undefined);
    };
    document.addEventListener('mousedown', openTerminalContextMenu, true);
    document.addEventListener('mousedown', closeTerminalContextMenu, true);
    document.addEventListener('mouseup', suppressTerminalRightMouseUp, true);
    document.addEventListener('contextmenu', suppressBrowserContextMenu, true);
    document.addEventListener('keydown', dismissTerminalContextMenu, true);
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
      const current = new WebSocket(conversationId
        ? agentTerminalUrl(runId, conversationId, terminal.rows, terminal.cols)
        : flowRunTerminalUrl(runId, terminal.rows, terminal.cols));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => { attempts = 0; setState('connected'); setDetail('已连接 active generation'); resize(); terminal.focus(); };
      current.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        if (event.code === 4401) {
          window.dispatchEvent(new Event('flowweave:auth-required'));
          return;
        }
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
    sendTerminalInput.current = data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); };
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      observer.disconnect();
      sendTerminalInput.current = () => undefined;
      removeForcedSelectionListeners?.();
      terminalScreen?.removeEventListener('mousedown', forceTextSelection, true);
      document.removeEventListener('mousedown', openTerminalContextMenu, true);
      document.removeEventListener('mousedown', closeTerminalContextMenu, true);
      document.removeEventListener('mouseup', suppressTerminalRightMouseUp, true);
      document.removeEventListener('contextmenu', suppressBrowserContextMenu, true);
      document.removeEventListener('keydown', dismissTerminalContextMenu, true);
      input.dispose();
      socket?.close(1000);
      terminal.dispose();
    };
  }, [conversationId, runId]);

  const closeMenu = () => setContextMenu(undefined);
  const copy = (value: string) => { closeMenu(); void copyTerminalText(value).catch(() => undefined); };
  const send = (value: string) => { closeMenu(); sendTerminalInput.current(value); };
  const selectedText = contextMenu?.text || '选中内容';
  return <div className={`agent-runtime-terminal ${standalone ? 'standalone' : ''}`}><div className="agent-terminal-status"><i className={state}/><span>{detail}</span></div><div ref={host} className="agent-terminal-screen" aria-label="Agent 运行终端"/>{contextMenu && createPortal(<div className="agent-terminal-context-menu" role="menu" aria-label="终端操作菜单" style={{ left: contextMenu.x, top: contextMenu.y }} onMouseDown={event => event.stopPropagation()} onContextMenu={event => event.preventDefault()}><button type="button" role="menuitem" disabled={!contextMenu.text} onClick={() => copy(contextMenu.text)}>复制 “{selectedText}”</button><button type="button" role="menuitem" disabled={!contextMenu.line} onClick={() => copy(contextMenu.line)}>复制当前行</button><button type="button" role="menuitem" disabled={!contextMenu.text} onClick={() => send(contextMenu.text)}>输入 “{selectedText}”</button><hr/><button type="button" role="menuitem" onClick={() => send('\u0002%')}>左右分屏</button><button type="button" role="menuitem" onClick={() => send('\u0002"')}>上下分屏</button><button type="button" role="menuitem" onClick={() => send('\u0002m')}>标记窗格</button></div>, document.body)}</div>;
}

export function FlowRunTerminalDialog({ runId, runName, onClose }: { runId: string; runName: string; onClose: () => void }) {
  useEscapeClose(onClose);
  return <div className="agent-terminal-overlay flow-run-terminal-dialog" role="dialog" aria-modal="true" aria-label={`${runName} 终端`} onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section onMouseDown={event => event.stopPropagation()}><header><div><Terminal size={18}/><span><b>{runName}</b><small>FlowRun 全局终端 · 右键可复制、输入或管理 tmux 窗格</small></span></div><button type="button" onClick={onClose}><X size={15}/>关闭</button></header><RuntimeTerminal runId={runId}/></section></div>;
}

function openStandaloneTerminal(runId: string, conversationId: string) {
  const url = new URL(window.location.href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('terminalRun', runId);
  url.searchParams.set('terminalConversation', conversationId);
  window.open(url, `flowweave-terminal-${conversationId}`, 'popup=yes,width=1280,height=820');
}

export function StandaloneAgentTerminal({ runId, runName, conversationId }: { runId: string; runName?: string | null; conversationId?: string | null }) {
  const title = conversationId ? 'Agent 运行终端' : `FlowRun · ${runName || '运行终端'}`;
  useEffect(() => { document.title = title; }, [title]);
  return <main className="standalone-terminal-page"><header><div><span className="eyebrow">FLOWRUN RUNTIME TERMINAL</span><h1>{title}</h1></div><div><span>连接始终经 FlowWeave 重新解析 active generation</span><button type="button" onClick={() => window.close()}><X size={16}/>关闭窗口</button></div></header><RuntimeTerminal runId={runId} conversationId={conversationId ?? undefined} standalone/></main>;
}

export function AgentRuntimeSidebar({ runId, conversation, runtime, children, governance, collapsed, onCollapsedChange }: Props) {
  const [tab, setTab] = useState<'context' | 'governance' | 'terminal'>('context');
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setTab('context'), [conversation?.id]);
  const terminal = conversation && runtime?.write_available ? <div className="agent-terminal-pane"><section className="agent-terminal-summary"><div className="agent-terminal-summary-title"><span><Terminal size={15}/></span><div><b>FlowRun Runtime 终端</b><small>active generation {runtime.active_generation ?? '—'}</small></div></div><div className="agent-terminal-actions"><button type="button" onClick={() => setExpanded(true)} aria-label="放大终端"><Maximize2 size={14}/></button><button type="button" onClick={() => openStandaloneTerminal(runId, conversation.id)} aria-label="在独立窗口中打开"><ExternalLink size={14}/></button></div></section><RuntimeTerminal runId={runId} conversationId={conversation.id}/></div> : <div className="empty compact">Runtime 当前不可写，终端已关闭。</div>;
  return <><aside className={`agent-runtime-sidebar ${collapsed ? 'collapsed' : ''}`}><header className="agent-sidebar-header"><div><span className="eyebrow">RUNTIME PANEL</span><b>运行工作台</b></div><nav aria-label="Agent 右侧栏"><button className={tab === 'context' ? 'active' : ''} onClick={() => setTab('context')}><Info size={13}/>上下文</button><button className={tab === 'governance' ? 'active' : ''} disabled={!governance} onClick={() => setTab('governance')}><Radar size={13}/>运维</button><button className={tab === 'terminal' ? 'active' : ''} disabled={!conversation} onClick={() => setTab('terminal')}><Terminal size={13}/>终端</button></nav><button type="button" className="agent-sidebar-toggle" onClick={() => onCollapsedChange(!collapsed)} aria-label={collapsed ? '展开运行工作台' : '收起运行工作台'}>{collapsed ? <PanelRightOpen size={16}/> : <PanelRightClose size={16}/>}</button></header><div className="agent-runtime-sidebar-body" aria-hidden={collapsed}>{tab === 'context' ? children : tab === 'governance' ? governance : terminal}</div></aside>{expanded && conversation && <div className="agent-terminal-overlay" role="dialog" aria-modal="true"><section><header><div><Terminal size={18}/><span><b>FlowRun Runtime 终端</b><small>物理 endpoint 不会暴露给客户端</small></span></div><div><button type="button" onClick={() => openStandaloneTerminal(runId, conversation.id)}><ExternalLink size={15}/>新窗口</button><button type="button" onClick={() => setExpanded(false)}><Minimize2 size={15}/>退出放大</button></div></header><RuntimeTerminal runId={runId} conversationId={conversation.id}/></section></div>}</>;
}
