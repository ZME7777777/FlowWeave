import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Info, LoaderCircle, Play, Search, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { EnvironmentVersion, EnvironmentVersionRunPage, TerminalEnvironment } from '../types';
import { useWorkbenchStore } from '../store/workbench';
import { Pagination } from './Pagination';
import { useProductDialog } from './ProductDialogContext';
import { useEscapeClose } from './useEscapeClose';
import './environment-versions.css';

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '未记录';
}
function commands(version: EnvironmentVersion): Array<[string, string]> {
  return Object.entries(record(version.manifest.commands)).filter((entry): entry is [string, string] => typeof entry[1] === 'string');
}
function protectedVersion(environment: TerminalEnvironment, version: EnvironmentVersion) {
  return version.reference_count > 0 || version.state === 'PUBLISHING'
    || environment.active_sessions.some(session => session.base_version_id === version.id);
}

function VersionRuns({ environmentId, versionId, onClose }: { environmentId: string; versionId: string; onClose: () => void }) {
  const focusRunDirectory = useWorkbenchStore(state => state.focusRunDirectory);
  const locate = (run: EnvironmentVersionRunPage['items'][number]) => { onClose(); focusRunDirectory(run.flow_definition_id, run.id); };
  const [page, setPage] = useState(1);
  const runs = useQuery({
    queryKey: ['environment-version-runs', environmentId, versionId, page],
    queryFn: ({ signal }) => api.environmentVersionRuns(environmentId, versionId, page, signal),
    staleTime: 0,
    retry: false,
  });
  const labels: Record<string, string> = { DRAFT: '草稿', ACTIVE: '进行中', RUNNING: '运行中', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消', PAUSED: '已暂停', WAITING_HUMAN: '等待人工处理' };
  return <section className="environment-version-runs" aria-label="使用此版本的 FlowRun">
    <p className="environment-version-detail-note">使用此环境版本的 FlowRun。{runs.data && `共 ${runs.data.total} 项。`}</p>
    {runs.isPending ? <div className="empty compact" role="status"><LoaderCircle size={18} className="spin"/>正在加载 FlowRun…</div>
      : runs.isError ? <div className="empty compact" role="alert"><span>加载 FlowRun 失败：{runs.error.message}</span><button className="secondary" onClick={() => void runs.refetch()}>重试</button></div>
      : <><div className="environment-version-detail-table-scroll"><table className="settings-data-table environment-version-runs-table"><thead><tr><th>FlowRun 名称与编号</th><th>状态</th><th>创建时间</th></tr></thead><tbody>{runs.data.items.map(run => <tr key={run.id} onClick={() => locate(run)}><td><button type="button" className="environment-version-run-link" aria-label={`定位 FlowRun ${run.name}`} title="在流程运行中定位此 FlowRun" onClick={event => { event.stopPropagation(); locate(run); }}><b>{run.name || `FlowRun #${run.run_no}`}</b><small>#{run.run_no} · {run.id}</small></button></td><td>{labels[run.state] || run.state}</td><td>{new Date(run.started_at).toLocaleString()}</td></tr>)}</tbody></table>{!runs.data.items.length && <div className="empty compact">此版本暂无 FlowRun 引用。</div>}</div>
      <footer className="environment-version-runs-footer"><Pagination page={page} pageSize={20} total={runs.data.total} onPageChange={setPage}/></footer></>}
  </section>;
}

function VersionDetails({ environment, version, onClose }: { environment: TerminalEnvironment; version: EnvironmentVersion; onClose: () => void }) {
  useEscapeClose(onClose);
  const [tab, setTab] = useState<'overview' | 'commands' | 'authentication' | 'runs'>('overview');
  const commandRows = commands(version);
  const build = record(version.manifest.build);
  const provenance = record(version.manifest.runtime_provenance);
  const packageVersions = Object.entries(record(provenance.package_versions)).filter((entry): entry is [string, string] => typeof entry[1] === 'string');
  return <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}><section className="modal environment-version-dialog" role="dialog" aria-modal="true" aria-label={`${environment.name} v${version.version_no} 版本详情`}>
    <header><div><h2>{environment.name} · v{version.version_no}</h2><small>{version.description || '未填写版本说明'}</small></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <nav role="tablist" aria-label="版本详情分类" onKeyDown={event => {
      const tabs = ['overview', 'commands', 'authentication', 'runs'] as const;
      const current = tabs.indexOf(tab);
      const next = event.key === 'ArrowRight' ? (current + 1) % tabs.length : event.key === 'ArrowLeft' ? (current + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
      if (next < 0) return;
      event.preventDefault(); setTab(tabs[next]); event.currentTarget.querySelectorAll<HTMLButtonElement>('button')[next]?.focus();
    }}><button role="tab" id="environment-version-overview-tab" aria-controls="environment-version-panel" tabIndex={tab === 'overview' ? 0 : -1} aria-selected={tab === 'overview'} onClick={() => setTab('overview')}>概览</button><button role="tab" id="environment-version-commands-tab" aria-controls="environment-version-panel" tabIndex={tab === 'commands' ? 0 : -1} aria-selected={tab === 'commands'} onClick={() => setTab('commands')}>命令（{commandRows.length}）</button><button role="tab" id="environment-version-authentication-tab" aria-controls="environment-version-panel" tabIndex={tab === 'authentication' ? 0 : -1} aria-selected={tab === 'authentication'} onClick={() => setTab('authentication')}>认证信息</button><button role="tab" id="environment-version-runs-tab" aria-controls="environment-version-panel" tabIndex={tab === 'runs' ? 0 : -1} aria-selected={tab === 'runs'} onClick={() => setTab('runs')}>FlowRun（{version.flow_run_reference_count ?? version.run_reference_count}）</button></nav>
    <div className="environment-version-dialog-body" id="environment-version-panel" role="tabpanel" aria-labelledby={`environment-version-${tab}-tab`}>
      {tab === 'overview' ? <div className="environment-version-detail-scroll"><dl className="settings-detail-facts"><dt>版本状态</dt><dd>{version.state}</dd><dt>发布时间</dt><dd>{new Date(version.created_at).toLocaleString()}</dd><dt>版本说明</dt><dd>{version.description || '未填写版本说明'}</dd><dt>运行能力</dt><dd>{version.runtime_capabilities?.join('、') || 'minimal'}</dd><dt>FlowRun 引用</dt><dd>{version.flow_run_reference_count ?? version.run_reference_count} 个 FlowRun</dd><dt>来源版本</dt><dd>{version.parent_version_id ? environment.versions.find(item => item.id === version.parent_version_id) ? `v${environment.versions.find(item => item.id === version.parent_version_id)!.version_no}` : version.parent_version_id : '基础镜像'}</dd><dt>镜像</dt><dd><code>{version.image_reference || '未生成'}</code></dd><dt>镜像摘要</dt><dd><code>{version.image_digest || '未生成'}</code></dd><dt>基础镜像</dt><dd><code>{version.base_image_reference || '未记录'}</code></dd><dt>平台</dt><dd>{text(build.platform ?? (version.manifest.os && version.manifest.architecture ? `${text(version.manifest.os)}/${text(version.manifest.architecture)}` : undefined))}</dd><dt>构建方式</dt><dd>{text(build.builder)}</dd>{Boolean(provenance.source_commit) && <><dt>Runtime 提交</dt><dd><code>{text(provenance.source_commit)}</code></dd></>}{packageVersions.length > 0 && <><dt>Runtime 包</dt><dd>{packageVersions.map(([name, value]) => <div key={name}>{name} · {value}</div>)}</dd></>}{version.error_detail && <><dt>失败信息</dt><dd className="error">{version.error_detail}</dd></>}</dl></div> : tab === 'runs' ? <VersionRuns environmentId={environment.id} versionId={version.id} onClose={onClose}/> : tab === 'commands' ? commandRows.length ? <><p className="environment-version-detail-note">发布时记录的命令探测结果；保留原始输出，错误或警告不代表有效版本号。</p><div className="environment-version-detail-table-scroll"><table className="settings-data-table environment-version-commands-table"><thead><tr><th>命令</th><th>探测结果</th></tr></thead><tbody>{commandRows.map(([name, value]) => <tr key={name}><td><code>{name}</code></td><td><pre>{value}</pre></td></tr>)}</tbody></table></div></> : <div className="empty compact">此版本未记录命令清单。</div> : <section className="environment-version-authentication"><Info size={19}/><div><b>此版本未记录认证清单，无法确认镜像中包含哪些认证信息。</b><p>平台“认证管理”的条目由会话另行选择和注入，不作为此镜像已包含认证的证据。</p></div></section>}
    </div><footer><button className="secondary" onClick={onClose}>完成</button></footer>
  </section></div>;
}

export function EnvironmentVersionsPanel({ environment, openingVersionId, draftBusy, onCreateDraft }: {
  environment: TerminalEnvironment; openingVersionId?: string | null; draftBusy: boolean;
  onCreateDraft: (version: EnvironmentVersion) => void;
}) {
  const dialog = useProductDialog();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [viewingId, setViewingId] = useState<string>();
  const [deleting, setDeleting] = useState(false);
  const [progress, setProgress] = useState({ processed: 0, total: 0 });
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const pageSize = 20;
  const visible = useMemo(() => environment.versions.filter(item => !query.trim() || `v${item.version_no} ${item.description} ${item.state} ${item.runtime_capabilities?.join(' ')}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [environment.versions, query]);
  const currentPage = Math.min(page, Math.max(1, Math.ceil(visible.length / pageSize)));
  const paged = visible.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const selectable = visible.filter(item => !protectedVersion(environment, item));
  const allSelected = selectable.length > 0 && selectable.every(item => selected.has(item.id));
  const viewing = environment.versions.find(item => item.id === viewingId);
  useEffect(() => { setSelected(current => { const next = new Set([...current].filter(id => environment.versions.some(item => item.id === id && !protectedVersion(environment, item)))); return next.size === current.size ? current : next; }); }, [environment]);
  const toggle = (id: string) => setSelected(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleAll = () => setSelected(current => { const next = new Set(current); selectable.forEach(item => { if (allSelected) next.delete(item.id); else next.add(item.id); }); return next; });
  const remove = async (versions: EnvironmentVersion[]) => {
    if (!versions.length || !await dialog.confirm({ title: versions.length === 1 ? `删除 ${environment.name} · v${versions[0].version_no}` : `删除所选 ${versions.length} 个环境版本？`, message: '此操作会删除版本记录并清理对应的本地镜像标签，无法撤销。仍被运行、冻结快照或配置会话引用的版本会保留；后续发布仍使用新的版本号。', confirmLabel: '删除版本', tone: 'danger' })) return;
    setDeleting(true); setNotice(''); setError(''); setProgress({ processed: 0, total: versions.length });
    const deleted = new Set<string>();
    const failures: string[] = [];
    try {
      for (const [index, version] of versions.entries()) {
        if (protectedVersion(environment, version)) failures.push(`v${version.version_no}：存在引用或正在发布`);
        else {
          try { await api.deleteEnvironmentVersion(environment.id, version.id); deleted.add(version.id); }
          catch (reason) {
            if (reason instanceof ApiError && reason.code === 'RESOURCE_NOT_FOUND') deleted.add(version.id);
            else if (reason instanceof ApiError && reason.code === 'ENVIRONMENT_VERSION_IN_USE') {
              const labels: Record<string, string> = { run_reference_count: '运行', snapshot_reference_count: '冻结快照', setup_reference_count: '配置会话', capability_validation_reference_count: '能力验证', mcp_oauth_secret_reference_count: 'MCP 认证引用' };
              const references = Object.entries(labels).filter(([key]) => Number(reason.details[key]) > 0).map(([key, label]) => `${reason.details[key]} 个${label}`);
              failures.push(`v${version.version_no}：${references.length ? references.join('、') : '其他记录'}正在引用，已保留`);
            } else failures.push(`v${version.version_no}：${reason instanceof Error ? reason.message : '删除失败'}`);
          }
        }
        setProgress({ processed: index + 1, total: versions.length });
      }
      setSelected(current => new Set([...current].filter(id => !deleted.has(id))));
      if (deleted.size) {
        queryClient.setQueryData<TerminalEnvironment[]>(['terminal-environments'], current => current?.map(item => item.id === environment.id ? { ...item, versions: item.versions.filter(version => !deleted.has(version.id)) } : item));
        setNotice(`已删除 ${deleted.size} 个环境版本。`);
      }
      if (failures.length) setError(`${failures.length} 个版本未删除，已保留。${failures.join('；')}`);
      await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
    } finally { setDeleting(false); }
  };
  return <section className="environment-versions-panel" aria-label="环境版本列表">
    <div className="environment-versions-toolbar"><b>历史版本 <span>{environment.versions.length}</span></b><label><Search size={14}/><input aria-label="搜索历史版本" placeholder="搜索版本、说明或状态…" value={query} onChange={event => { setQuery(event.target.value); setPage(1); }}/></label><button className="danger" disabled={!selected.size || deleting} onClick={() => void remove(environment.versions.filter(item => selected.has(item.id)))}><Trash2 size={13}/>{deleting ? `删除中 ${progress.processed}/${progress.total}` : `删除所选 (${selected.size})`}</button></div>
    {notice && <div className="notice success" role="status">{notice}</div>}{error && <div className="notice error" role="alert">{error}</div>}
    <div className="environment-versions-table-scroll" key={`${query}-${currentPage}`}><table className="settings-data-table environment-versions-table"><thead><tr><th><input type="checkbox" aria-label="选择全部可删除版本" title="选择全部匹配结果（含其他页），跳过被引用和发布中的版本" checked={allSelected} disabled={!selectable.length || deleting} ref={node => { if (node) node.indeterminate = !allSelected && selectable.some(item => selected.has(item.id)); }} onChange={toggleAll}/></th><th>版本与说明</th><th>状态</th><th>发布时间</th><th>FlowRun</th><th>命令</th><th>操作</th></tr></thead><tbody>{paged.map(version => { const protectedItem = protectedVersion(environment, version); const canDraft = version.state === 'READY' && !environment.active_sessions.length; return <tr key={version.id} className={selected.has(version.id) ? 'selected' : ''} tabIndex={0} onKeyDown={event => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); setViewingId(version.id); } }} onClick={event => { if (!(event.target as HTMLElement).closest('button, input, a, label')) setViewingId(version.id); }}><td><input type="checkbox" aria-label={`选择版本 v${version.version_no}`} title={protectedItem ? '此版本存在引用或正在发布，不能删除' : '选择版本'} checked={selected.has(version.id)} disabled={protectedItem || deleting} onChange={() => toggle(version.id)}/></td><td><button className="environment-version-name" aria-label={`查看版本 v${version.version_no}`} onClick={() => setViewingId(version.id)}>v{version.version_no}</button><small title={version.description}>{version.description || '未填写版本说明'}</small></td><td><span className={`environment-version-state ${version.state.toLowerCase()}`}>{version.state}</span></td><td>{new Date(version.created_at).toLocaleString()}</td><td><span title={`全部引用：${version.reference_count}`}>{version.flow_run_reference_count ?? version.run_reference_count} 个 FlowRun</span></td><td>{commands(version).length}</td><td><div className="environment-version-row-actions"><button className="secondary" aria-label={`从 v${version.version_no} 创建草稿`} title={!canDraft ? version.state !== 'READY' ? '仅已就绪版本可创建草稿' : '请先完成或丢弃当前配置会话' : `以 v${version.version_no} 为基础创建独立配置草稿`} disabled={!canDraft || draftBusy || deleting} onClick={() => onCreateDraft(version)}>{openingVersionId === version.id ? <LoaderCircle size={12} className="spin"/> : <Play size={12}/>}创建草稿</button><button className="ghost" aria-label={`删除版本 v${version.version_no}`} title={protectedItem ? '解除引用并等待发布完成后才能删除' : `删除 v${version.version_no}`} disabled={protectedItem || deleting} onClick={() => void remove([version])}><Trash2 size={14}/></button></div></td></tr>; })}</tbody></table>{!visible.length && <div className="empty"><b>{query ? '没有匹配的版本' : '还没有已发布版本'}</b><span>{query ? '调整搜索条件查看其他版本。' : '开启终端配置环境后发布。'}</span></div>}</div>
    <footer className="environment-versions-footer"><span>已选 {selected.size} / {visible.length} 个版本</span><Pagination page={currentPage} pageSize={pageSize} total={visible.length} onPageChange={setPage}/></footer>
    {viewing && <VersionDetails key={viewing.id} environment={environment} version={viewing} onClose={() => setViewingId(undefined)}/>}
  </section>;
}
