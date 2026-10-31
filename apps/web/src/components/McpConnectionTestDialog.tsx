import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { api } from '../api/client';
import type { AgentSessionMcpReadiness, CapabilityAsset } from '../types';
import { useDialogFocus } from './useDialogFocus';
import { useEscapeClose } from './useEscapeClose';
import { mcpConnectionLabel } from './mcp-connection';
import './mcp-details.css';

export function McpConnectionTestDialog({ targets, onResult, onClose }: {
  targets: CapabilityAsset[];
  onResult: (id: string, result: AgentSessionMcpReadiness) => void;
  onClose: () => void;
}) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus();
  const [results, setResults] = useState<Record<string, AgentSessionMcpReadiness>>({});
  const [current, setCurrent] = useState<string>();
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState('');
  const report = useRef(onResult);
  const completed = useRef(new Set<string>());
  report.current = onResult;
  useEscapeClose(onClose);
  useEffect(() => {
    const controller = new AbortController();
    const run = async () => {
      if (completed.current.size === targets.length) return;
      try {
        const workspace = await api.defaultAgentWorkspace(controller.signal);
        for (const item of targets) {
          if (controller.signal.aborted) return;
          if (completed.current.has(item.id)) continue;
          setCurrent(item.id);
          let result: AgentSessionMcpReadiness;
          try {
            result = await api.agentWorkspaceMcpReadiness(workspace.id, item.id, controller.signal);
          } catch {
            if (controller.signal.aborted) return;
            result = { state: 'UNAVAILABLE', error_kind: 'unknown', checked_at: new Date().toISOString() };
          }
          if (controller.signal.aborted) return;
          completed.current.add(item.id);
          setResults(previous => ({ ...previous, [item.id]: result }));
          report.current(item.id, result);
        }
        setCurrent(undefined);
        setComplete(true);
      } catch {
        if (!controller.signal.aborted) setError('无法取得当前 Agent 运行环境，请进入 AI 工作台检查运行环境后重试。');
      }
    };
    void run();
    return () => controller.abort();
  }, [targets]);
  const ready = Object.values(results).filter(item => item.state === 'READY').length;
  return <div className="modal-backdrop mcp-details-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} onKeyDown={onDialogKeyDown} className="modal mcp-connection-dialog" role="dialog" aria-modal="true" aria-label="测试 MCP 连接">
      <header><h2>测试 MCP 连接</h2><span role="status">{complete ? `完成 · ${ready}/${targets.length} 已连接` : `${Object.keys(results).length}/${targets.length} 已检测`}</span><button type="button" className="ghost" aria-label="关闭连接测试" autoFocus onClick={onClose}><X size={17}/></button></header>
      <p>从当前 Agent 运行环境检测连接并获取工具列表，不执行工具。</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="mcp-connection-scroll"><table><thead><tr><th>MCP</th><th>连接状态</th><th>工具</th></tr></thead><tbody>{targets.map(item => {
        const result = results[item.id];
        return <tr key={item.id}><td><b>{item.capability_key}</b><small>v{item.revision_number}{item.is_builtin ? ' · 内置' : ''}</small></td><td className={result?.state === 'READY' ? 'mcp-test-ready' : result ? 'mcp-test-failed' : ''}>{result ? mcpConnectionLabel(result) : current === item.id ? '检测中…' : '等待检测'}</td><td>{result?.state === 'READY' ? result.tools?.length ? <ul>{result.tools.map(tool => <li key={tool}><code>{tool}</code></li>)}</ul> : '连接成功，未提供工具' : '—'}</td></tr>;
      })}</tbody></table></div>
      <footer><span>结果对应本次环境与固定版本；连接成功不代表所有工具调用均成功。</span><button type="button" className="secondary" onClick={onClose}>{complete || error ? '关闭' : '停止后续检测'}</button></footer>
    </section>
  </div>;
}
