import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Code2, FileText, LoaderCircle, Pencil, PlugZap, X } from 'lucide-react';
import { api } from '../api/client';
import type { AgentSessionMcpReadiness, CapabilityAsset, CapabilityLineage } from '../types';
import { mcpConnectionLabel } from './mcp-connection';
import { useDialogFocus } from './useDialogFocus';
import { useEscapeClose } from './useEscapeClose';
import { McpAuthenticationPanel } from './McpAuthenticationPanel';
import './mcp-details.css';

export function McpDetailsDialog({ group, onClose, onEdit, onResult, onAuthenticationSaved, results = {}, initialTab = 'config' }: {
  group: CapabilityLineage; onClose: () => void;
  results?: Record<string, AgentSessionMcpReadiness>;
  initialTab?: 'config' | 'tools';
  onEdit: (source: CapabilityAsset, latest: CapabilityAsset) => void;
  onResult: (id: string, result: AgentSessionMcpReadiness) => void;
  onAuthenticationSaved: (id: string) => void;
}) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus();
  const [selectedId, setSelectedId] = useState(group.latest.id);
  const [tab, setTab] = useState<'config' | 'json' | 'scripts' | 'tools' | 'auth'>(initialTab);
  const [authenticationOpened, setAuthenticationOpened] = useState(false);
  const versions = [...group.versions].sort((a, b) => b.revision_number - a.revision_number);
  const selected = versions.find(item => item.id === selectedId) ?? group.latest;
  const testController = useRef<AbortController | undefined>(undefined);
  const [testingVersionId, setTestingVersionId] = useState<string>();
  const [testError, setTestError] = useState<{ versionId: string; message: string }>();
  const testing = testingVersionId === selected.id;
  const [readOnlyTool, setReadOnlyTool] = useState('');
  const [toolArguments, setToolArguments] = useState('{}');
  useEffect(() => () => testController.current?.abort(), [selected.id]);
  const testConnection = async () => {
    testController.current?.abort();
    const controller = new AbortController();
    testController.current = controller;
    const versionId = selected.id;
    setTestingVersionId(versionId); setTestError(undefined); setTab('tools');
    try {
      let call: { name: string; arguments: Record<string, unknown> } | undefined;
      if (readOnlyTool.trim()) {
        const args: unknown = JSON.parse(toolArguments);
        if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('工具参数须为 JSON 对象。');
        call = { name: readOnlyTool.trim(), arguments: args as Record<string, unknown> };
      }
      const workspace = await api.defaultAgentWorkspace(controller.signal);
      let response: AgentSessionMcpReadiness;
      try { response = await api.agentWorkspaceMcpReadiness(workspace.id, versionId, controller.signal, call); }
      catch {
        if (controller.signal.aborted) return;
        response = { state: 'UNAVAILABLE', error_kind: 'unknown', checked_at: new Date().toISOString() };
      }
      if (!controller.signal.aborted) onResult(versionId, response);
    } catch (reason) {
      if (!controller.signal.aborted) setTestError({ versionId, message: reason instanceof SyntaxError ? '工具参数须为有效 JSON 对象。' : reason instanceof Error ? reason.message : '无法取得当前 Agent 运行环境，请进入 AI 工作台检查运行环境后重试。' });
    } finally {
      if (testController.current === controller) {
        testController.current = undefined;
        setTestingVersionId(undefined);
      }
    }
  };
  const source = useQuery({ queryKey: ['mcp-source', selected.id], queryFn: () => api.mcpSource(selected.id), retry: false });
  useEscapeClose(onClose);
  const config = selected.document;
  const result = testing || testError?.versionId === selected.id ? undefined : results[selected.id];
  const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '—';
  const fields = Object.entries(config).filter(([key]) => !['capability_id', 'capability_version_id', 'storage_key', 'script_archive_prefix', 'script_files'].includes(key));
  return <div className="modal-backdrop mcp-details-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} onKeyDown={onDialogKeyDown} className="modal mcp-details-dialog" role="dialog" aria-modal="true" aria-label={`MCP ${group.latest.capability_key} 详情`}>
      <header><h2>{group.latest.capability_key}</h2><span>MCP{group.latest.is_builtin ? ' · 内置' : ''} · {versions.length} 个版本</span><button type="button" className="ghost" aria-label="关闭 MCP 详情" autoFocus onClick={onClose}><X size={17}/></button></header>
      <div className="mcp-details-layout">
        <aside aria-label="MCP 版本历史"><header>版本历史</header><div>{versions.map(version => <button type="button" key={version.id} className={selected.id === version.id ? 'active' : ''} aria-pressed={selected.id === version.id} onClick={() => setSelectedId(version.id)}><b>v{version.revision_number}<em>{version.id === group.latest.id ? '当前' : '历史'}</em></b><small>{new Date(version.created_at).toLocaleString()}</small></button>)}</div></aside>
        <main><div className="mcp-details-version"><b>v{selected.revision_number}</b><span>{selected.id === group.latest.id ? '当前版本' : '历史版本'} · {new Date(selected.created_at).toLocaleString()}</span><button type="button" className="secondary mcp-details-test" disabled={testing} onClick={() => void testConnection()}>{testing ? <LoaderCircle size={14} className="spin"/> : <PlugZap size={14}/>} {testing ? '测试中…' : '测试连接'}</button></div>
          <nav aria-label="MCP 版本内容">{([['config', '配置'], ['auth', '认证'], ['tools', '工具能力'], ['json', 'JSON'], ['scripts', `脚本（${source.data?.mcp_scripts.length ?? 0}）`]] as const).map(([value, label]) => <button type="button" key={value} className={tab === value ? 'active' : ''} aria-pressed={tab === value} onClick={() => { setTab(value); if (value === 'auth') setAuthenticationOpened(true); }}>{label}</button>)}</nav>
          <div className="mcp-details-content">
            {authenticationOpened && <div hidden={tab !== 'auth'}><McpAuthenticationPanel key={selected.id} versionId={selected.id} url={String(config.url ?? '')} onSaved={() => { testController.current?.abort(); onAuthenticationSaved(selected.id); }}/></div>}{tab === 'auth' ? null : tab === 'tools' ? <section className="mcp-tool-catalog">{!group.latest.is_builtin && <details className="mcp-tool-invocation"><summary>附加只读工具验证（可选）</summary><label><span>工具名称</span><input aria-label="只读工具名称" value={readOnlyTool} placeholder="例如 search_files" disabled={testing} onChange={event => setReadOnlyTool(event.target.value)}/></label><label><span>参数 JSON</span><textarea aria-label="只读工具参数" value={toolArguments} disabled={testing} onChange={event => setToolArguments(event.target.value)}/></label><p>填写后点击“测试连接”将实际调用该工具，请仅选择读取操作。</p></details>}{result?.read_only_tool_call && <p role="status">{result.read_only_tool_call.is_error === true ? '工具调用失败' : result.read_only_tool_call.is_error === false ? '工具已返回' : '工具未执行'}{result.read_only_tool_call.business_code !== null ? ` · 业务码 ${result.read_only_tool_call.business_code}` : ''} · {result.read_only_tool_call.name}</p>}<header><b role="status">{testing ? '检测中…' : mcpConnectionLabel(result)}</b>{result && <small>检测于 {new Date(result.checked_at).toLocaleString()}</small>}</header>{testing ? <p>{readOnlyTool.trim() ? '正在检测连接并验证指定的只读工具。' : '正在检测此版本的连接并获取工具列表，不执行工具。'}</p> : testError?.versionId === selected.id ? <p role="alert">{testError.message}</p> : result?.state === 'READY' ? result.tools?.length ? <ul>{result.tools.map(name => <li key={name}><code>{name}</code>{name === 'read_referenced_conversation' && <span>按授权读取引用会话的消息，支持分页、搜索和完整正文分段读取。</span>}</li>)}</ul> : <p>服务连接成功，未提供工具。</p> : <p>点击“测试连接”，获取此版本实际可用的工具列表。</p>}</section> : source.isPending ? <div className="empty compact" role="status">正在读取版本内容…</div> : source.isError ? <div className="empty compact" role="alert"><span>{source.error.message}</span><button type="button" className="secondary" onClick={() => void source.refetch()}>重试</button></div> : tab === 'json' ? <pre aria-label="MCP 版本 JSON">{source.data.content}</pre> : tab === 'scripts' ? source.data.mcp_scripts.length ? <table><thead><tr><th>文件</th><th>绑定 Server</th><th>大小</th></tr></thead><tbody>{source.data.mcp_scripts.map(script => <tr key={script.filename}><td><FileText size={13}/>{script.filename}</td><td>{script.server}</td><td>{Math.max(0, Math.floor(script.content_base64.length * 3 / 4) - (script.content_base64.endsWith('==') ? 2 : script.content_base64.endsWith('=') ? 1 : 0))} B</td></tr>)}</tbody></table> : <div className="empty compact">此版本没有脚本资源。</div> : <><table><thead><tr><th>配置项</th><th>值</th></tr></thead><tbody>{fields.map(([key, value]) => <tr key={key}><td>{key}</td><td><pre>{text(value)}</pre></td></tr>)}</tbody></table><details><summary><Code2 size={13}/>版本标识</summary><dl><dt>Version ID</dt><dd>{selected.id}</dd><dt>Digest</dt><dd>{selected.digest || selected.content_hash}</dd><dt>来源文件</dt><dd>{selected.filename}</dd></dl></details></>}
          </div>
        </main>
      </div>
      <footer><span>{group.latest.is_builtin ? '系统内置 · 仅供查看与测试' : '发布新版本后，已有会话和运行继续使用冻结版本。'}</span><button type="button" className="secondary" onClick={onClose}>关闭</button>{!group.latest.is_builtin && <button type="button" className="primary" disabled={!source.data || source.isError} onClick={() => onEdit(selected, group.latest)}><Pencil size={13}/>基于 v{selected.revision_number} 编辑</button>}</footer>
    </section>
  </div>;
}
