import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Code2, FileText, Pencil, X } from 'lucide-react';
import { api } from '../api/client';
import type { CapabilityAsset, CapabilityLineage } from '../types';
import { useDialogFocus } from './useDialogFocus';
import { useEscapeClose } from './useEscapeClose';
import './mcp-details.css';

export function McpDetailsDialog({ group, onClose, onEdit }: {
  group: CapabilityLineage; onClose: () => void;
  onEdit: (source: CapabilityAsset, latest: CapabilityAsset) => void;
}) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus();
  const [selectedId, setSelectedId] = useState(group.latest.id);
  const [tab, setTab] = useState<'config' | 'json' | 'scripts'>('config');
  const versions = [...group.versions].sort((a, b) => b.revision_number - a.revision_number);
  const selected = versions.find(item => item.id === selectedId) ?? group.latest;
  const source = useQuery({ queryKey: ['mcp-source', selected.id], queryFn: () => api.mcpSource(selected.id), retry: false });
  useEscapeClose(onClose);
  const config = selected.document;
  const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '—';
  const fields = Object.entries(config).filter(([key]) => !['capability_id', 'capability_version_id', 'storage_key', 'script_archive_prefix', 'script_files'].includes(key));
  return <div className="modal-backdrop mcp-details-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} onKeyDown={onDialogKeyDown} className="modal mcp-details-dialog" role="dialog" aria-modal="true" aria-label={`MCP ${group.latest.capability_key} 详情`}>
      <header><h2>{group.latest.capability_key}</h2><span>MCP · {versions.length} 个版本</span><button type="button" className="ghost" aria-label="关闭 MCP 详情" autoFocus onClick={onClose}><X size={17}/></button></header>
      <div className="mcp-details-layout">
        <aside aria-label="MCP 版本历史"><header>版本历史</header><div>{versions.map(version => <button type="button" key={version.id} className={selected.id === version.id ? 'active' : ''} aria-pressed={selected.id === version.id} onClick={() => setSelectedId(version.id)}><b>v{version.revision_number}<em>{version.id === group.latest.id ? '当前' : '历史'}</em></b><small>{new Date(version.created_at).toLocaleString()}</small></button>)}</div></aside>
        <main><div className="mcp-details-version"><b>v{selected.revision_number}</b><span>{selected.id === group.latest.id ? '当前版本' : '历史版本'} · {new Date(selected.created_at).toLocaleString()}</span></div>
          <nav aria-label="MCP 版本内容">{([['config', '配置'], ['json', 'JSON'], ['scripts', `脚本（${source.data?.mcp_scripts.length ?? 0}）`]] as const).map(([value, label]) => <button type="button" key={value} className={tab === value ? 'active' : ''} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</nav>
          <div className="mcp-details-content">
            {source.isPending ? <div className="empty compact" role="status">正在读取版本内容…</div> : source.isError ? <div className="empty compact" role="alert"><span>{source.error.message}</span><button type="button" className="secondary" onClick={() => void source.refetch()}>重试</button></div> : tab === 'json' ? <pre aria-label="MCP 版本 JSON">{source.data.content}</pre> : tab === 'scripts' ? source.data.mcp_scripts.length ? <table><thead><tr><th>文件</th><th>绑定 Server</th><th>大小</th></tr></thead><tbody>{source.data.mcp_scripts.map(script => <tr key={script.filename}><td><FileText size={13}/>{script.filename}</td><td>{script.server}</td><td>{Math.max(0, Math.floor(script.content_base64.length * 3 / 4) - (script.content_base64.endsWith('==') ? 2 : script.content_base64.endsWith('=') ? 1 : 0))} B</td></tr>)}</tbody></table> : <div className="empty compact">此版本没有脚本资源。</div> : <><table><thead><tr><th>配置项</th><th>值</th></tr></thead><tbody>{fields.map(([key, value]) => <tr key={key}><td>{key}</td><td><pre>{text(value)}</pre></td></tr>)}</tbody></table><details><summary><Code2 size={13}/>版本标识</summary><dl><dt>Version ID</dt><dd>{selected.id}</dd><dt>Digest</dt><dd>{selected.digest || selected.content_hash}</dd><dt>来源文件</dt><dd>{selected.filename}</dd></dl></details></>}
          </div>
        </main>
      </div>
      <footer><span>发布新版本后，已有会话和运行继续使用冻结版本。</span><button type="button" className="secondary" onClick={onClose}>关闭</button><button type="button" className="primary" disabled={!source.data || source.isError || group.latest.is_builtin} onClick={() => onEdit(selected, group.latest)}><Pencil size={13}/>基于 v{selected.revision_number} 编辑</button></footer>
    </section>
  </div>;
}
