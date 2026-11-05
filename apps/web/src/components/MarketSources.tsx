import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Pencil, Plus, Settings2, Trash2, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { api, ApiError, randomId } from '../api/client';
import type { MarketSource, MarketSources } from '../types';
import { LaunchOptionMenu } from './LaunchOptionMenu';
import { useEscapeClose } from './useEscapeClose';
import { marketSourcesQueryKey, useMarketSources } from './useMarketSources';
import './market-sources.css';

type MarketKind = 'PLUGIN' | 'SKILL';
const belongsTo = (source: MarketSource, kind: MarketKind) => kind === 'PLUGIN' ? source.kind === 'PLUGIN_GIT' : source.kind !== 'PLUGIN_GIT';
// Compare this market only. Saving a Skill edit must preserve newer Plugin edits,
// while competing edits to the same market still require user reconciliation.
const signature = (value: MarketSources, kind: MarketKind) => JSON.stringify(value.sources.filter(item => belongsTo(item, kind)).map(item => [item.id, item.name, item.kind, item.url, item.ref, item.repo_path, item.enabled, item.builtin]).sort((a, b) => String(a[0]).localeCompare(String(b[0]))));

export function MarketSourcePicker({ market, disabled }: { market: ReturnType<typeof useMarketSources>; disabled: boolean }) {
  const [manage, setManage] = useState(false);
  return <div className="market-source-picker">
    <LaunchOptionMenu label="市场来源" value={market.source?.id ?? ''} disabled={disabled || !market.sources.length} placeholder={market.isPending ? '正在加载来源…' : '没有启用的来源'} options={market.sources.map(item => ({ value: item.id, label: `${item.name}${item.builtin ? ' · 内置' : ''}` }))} onChange={market.setSelectedId}/>
    {market.source && <a href={market.source.url} target="_blank" rel="noreferrer" title={market.source.url}>{market.source.url}</a>}
    <button className="ghost" disabled={disabled} onClick={() => setManage(true)}><Settings2 size={14}/>市场源管理</button>
    {market.isError && <p role="alert">来源加载失败 <button className="ghost" onClick={() => void market.refetch()}>重试加载来源</button></p>}
    {manage && <MarketSourcesDialog kind={market.kind} onClose={() => setManage(false)}/>}
  </div>;
}

export function MarketSourcesDialog({ kind, onClose }: { kind: MarketKind; onClose: () => void }) {
  const qc = useQueryClient();
  const queryKey = marketSourcesQueryKey;
  const query = useQuery({ queryKey, queryFn: ({ signal }) => api.marketSources(signal), staleTime: 0 });
  const [draft, setDraft] = useState<MarketSources>();
  const baseline = useRef<string | undefined>(undefined);
  const [editingId, setEditingId] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEscapeClose(onClose, !busy);
  const value = draft ?? query.data;
  const sources = value?.sources.filter(item => belongsTo(item, kind)) ?? [];
  const editing = sources.find(item => item.id === editingId);
  const title = kind === 'PLUGIN' ? 'Plugin 市场源' : 'Skill 市场源';
  const update = (next: MarketSources) => {
    if (!draft && value) baseline.current = signature(value, kind);
    setDraft(next); setError('');
  };
  const change = (id: string, patch: Partial<MarketSource>) => { if (value) update({ ...value, sources: value.sources.map(item => item.id === id ? { ...item, ...patch } : item) }); };
  const reload = async () => { setDraft(undefined); baseline.current = undefined; setEditingId(undefined); setError(''); await query.refetch(); };
  const save = async () => {
    if (!value) return;
    if (!draft) { onClose(); return; }
    setBusy(true); setError('');
    try {
      await qc.cancelQueries({ queryKey });
      for (let attempt = 0; attempt < 2; attempt++) {
        const latest = await api.marketSources();
        if (signature(latest, kind) !== baseline.current) throw new Error(`${title}已在其他页面更新，请重新加载后再修改。`);
        const merged = { ...latest, sources: [...latest.sources.filter(item => !belongsTo(item, kind)), ...sources] };
        try {
          const result = await api.saveMarketSources(merged);
          qc.setQueryData(queryKey, result); onClose(); return;
        } catch (reason) {
          if (attempt === 0 && reason instanceof ApiError && reason.code === 'MARKET_SOURCES_CONFLICT') continue;
          throw reason;
        }
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { setBusy(false); }
  };
  const add = () => {
    if (!value) return;
    const source: MarketSource = { id: `custom-${randomId()}`, name: '', kind: kind === 'PLUGIN' ? 'PLUGIN_GIT' : 'SKILL_GIT', url: '', ref: 'HEAD', repo_path: '', enabled: true, builtin: false };
    update({ ...value, sources: [...value.sources, source] }); setEditingId(source.id);
  };
  const remove = (source: MarketSource) => {
    if (!value || source.builtin) return;
    update({ ...value, sources: value.sources.filter(item => item.id !== source.id) });
    if (editingId === source.id) setEditingId(undefined);
  };
  return <div className="modal-backdrop market-sources-backdrop" onClick={event => { event.stopPropagation(); if (!busy && event.target === event.currentTarget) onClose(); }}><section className="modal market-sources-dialog" role="dialog" aria-modal="true" aria-label={title}>
    <header><h2>{title}<small>{sources.length} 项</small></h2><button className="secondary" disabled={busy || !value || value.sources.length >= 30} onClick={add}><Plus size={14}/>添加来源</button><button className="ghost" aria-label={`关闭${title}`} disabled={busy} onClick={onClose}><X size={17}/></button></header>
    {query.isPending && <div className="empty compact" role="status">正在加载来源…</div>}{query.isError && <div className="notice error" role="alert">来源加载失败<button className="secondary" onClick={() => void query.refetch()}>重试</button></div>}
    <div className="market-sources-content"><table className="market-sources-table"><thead><tr><th>名称与地址</th><th>来源</th><th>状态</th><th>操作</th></tr></thead><tbody>{sources.map(source => <tr key={source.id} className={editingId === source.id ? 'active' : ''}><td><b>{source.name || '未命名来源'}</b><small title={source.url}>{source.url || '待填写仓库地址'}</small></td><td>{source.builtin ? '内置' : '自定义'}</td><td><button type="button" className="market-source-toggle" role="switch" aria-label={`启用来源 ${source.name || '未命名来源'}`} aria-checked={source.enabled} disabled={busy} onClick={() => change(source.id, { enabled: !source.enabled })}><span/>{source.enabled ? '已启用' : '已停用'}</button></td><td>{!source.builtin && <><button className="ghost" aria-label={`编辑来源 ${source.name || '未命名来源'}`} disabled={busy} onClick={() => setEditingId(editingId === source.id ? undefined : source.id)}><Pencil size={14}/></button><button className="ghost" aria-label={`删除来源 ${source.name || '未命名来源'}`} disabled={busy} onClick={() => remove(source)}><Trash2 size={14}/></button></>}</td></tr>)}</tbody></table>
      {editing && <section className="market-source-editor" aria-label="编辑市场源"><header><b>{editing.name || '新建来源'}</b><button className="ghost" aria-label="收起来源编辑" onClick={() => setEditingId(undefined)}><X size={14}/></button></header><div><label>名称<input autoFocus aria-label="来源名称" maxLength={80} disabled={busy} value={editing.name} onChange={event => change(editing.id, { name: event.target.value })}/></label><label>仓库地址<input aria-label="来源地址" disabled={busy} maxLength={500} value={editing.url} placeholder="https://github.com/owner/repo.git" onChange={event => change(editing.id, { url: event.target.value })}/></label><label>分支、标签或提交号<input aria-label="来源版本" disabled={busy} maxLength={120} value={editing.ref} onChange={event => change(editing.id, { ref: event.target.value })}/></label><label>仓库内目录（可选）<input aria-label="来源目录" disabled={busy} maxLength={300} value={editing.repo_path} placeholder={kind === 'PLUGIN' ? 'plugins' : 'skills'} onChange={event => change(editing.id, { repo_path: event.target.value })}/></label></div></section>}
    </div>
    {error && <div className="notice error" role="alert">{error}<button className="ghost" disabled={busy} onClick={() => void reload()}>重新加载</button></div>}
    <footer><span>{kind === 'PLUGIN' ? 'Plugin Git 仓库' : 'Skill Git 仓库与公开搜索'} · 当前账户</span><button className="ghost" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy || !value || !sources.length} onClick={() => void save()}>{busy ? '保存中…' : '保存来源'}</button></footer>
  </section></div>;
}
