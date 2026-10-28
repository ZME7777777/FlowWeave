import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Settings2, Trash2, X } from 'lucide-react';
import { useState } from 'react';
import { api, randomId } from '../api/client';
import type { MarketSource, MarketSources } from '../types';
import { useEscapeClose } from './useEscapeClose';
import { marketSourcesQueryKey, useMarketSources } from './useMarketSources';
import './market-sources.css';

const queryKey = marketSourcesQueryKey;
const labels = { PLUGIN_GIT: 'Plugin Git 仓库', SKILL_GIT: 'Skill Git 仓库', SKILLS_SH: 'Skill 公开搜索' };

export function MarketSourcePicker({ market, disabled }: { market: ReturnType<typeof useMarketSources>; disabled: boolean }) {
  const [manage, setManage] = useState(false);
  return <div className="market-source-picker">
    <div><label>市场来源<select aria-label="市场来源" value={market.source?.id ?? ''} disabled={disabled || !market.sources.length} onChange={event => market.setSelectedId(event.target.value)}>{!market.sources.length && <option value="">{market.isPending ? '正在加载来源…' : '没有启用的来源'}</option>}{market.sources.map(item => <option key={item.id} value={item.id}>{item.name}{item.builtin ? ' · 内置' : ''}</option>)}</select></label><button className="ghost" disabled={disabled} onClick={() => setManage(true)}><Settings2 size={14}/>市场源管理</button></div>
    {market.source && <a href={market.source.url} target="_blank" rel="noreferrer">{market.source.url}</a>}
    {market.isError && <p role="alert">来源加载失败 <button className="ghost" onClick={() => void market.refetch()}>重试加载来源</button></p>}
    {manage && <MarketSourcesDialog onClose={() => setManage(false)}/>}
  </div>;
}

export function MarketSourcesDialog({ onClose }: { onClose: () => void }) {
  useEscapeClose(onClose);
  const qc = useQueryClient();
  const query = useQuery({ queryKey, queryFn: ({ signal }) => api.marketSources(signal), staleTime: 0 });
  const [draft, setDraft] = useState<MarketSources>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const value = draft ?? query.data;
  const change = (id: string, patch: Partial<MarketSource>) => { if (value) setDraft({ ...value, sources: value.sources.map(item => item.id === id ? { ...item, ...patch } : item) }); };
  const save = async () => {
    if (!value) return;
    setBusy(true); setError('');
    try { await qc.cancelQueries({ queryKey }); const result = await api.saveMarketSources(value); qc.setQueryData(queryKey, result); onClose(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop market-sources-backdrop" onClick={event => event.stopPropagation()}><section className="modal market-sources-dialog" role="dialog" aria-modal="true" aria-label="市场源管理">
    <header><div><h2>市场源管理</h2><p>Plugin 与 Skill 统一管理，配置保存到当前账户。</p></div><button className="ghost" aria-label="关闭市场源管理" onClick={onClose}><X size={17}/></button></header>
    <div className="market-sources-content">
      {query.isPending && <p role="status">正在加载来源…</p>}{query.isError && <p role="alert">来源加载失败 <button onClick={() => void query.refetch()}>重试</button></p>}
      {value?.sources.map(item => <fieldset key={item.id} disabled={busy} className="market-source-card"><legend>{item.builtin ? '内置来源' : '自定义来源'} · {labels[item.kind]}</legend>
        <div className="market-source-title"><label><input type="checkbox" aria-label={`启用来源 ${item.name}`} checked={item.enabled} onChange={event => change(item.id, { enabled: event.target.checked })}/>启用</label>{!item.builtin && <button className="ghost" aria-label={`删除来源 ${item.name}`} onClick={() => setDraft({ ...value, sources: value.sources.filter(source => source.id !== item.id) })}><Trash2 size={14}/></button>}</div>
        <label>名称<input aria-label={`来源名称 ${item.id}`} value={item.name} readOnly={item.builtin} onChange={event => change(item.id, { name: event.target.value })}/></label>
        {!item.builtin && <label>类型<select aria-label={`来源类型 ${item.id}`} value={item.kind} onChange={event => change(item.id, { kind: event.target.value as MarketSource['kind'] })}><option value="PLUGIN_GIT">Plugin Git 仓库</option><option value="SKILL_GIT">Skill Git 仓库</option></select></label>}
        <label>来源地址<input aria-label={`来源地址 ${item.id}`} value={item.url} readOnly={item.builtin} placeholder="https://github.com/owner/repo.git" onChange={event => change(item.id, { url: event.target.value })}/></label>
        {!item.builtin && <div className="market-source-fields"><label>分支、标签或完整提交号<input value={item.ref} onChange={event => change(item.id, { ref: event.target.value })}/></label><label>仓库内目录（可选）<input value={item.repo_path} placeholder="skills 或 plugins" onChange={event => change(item.id, { repo_path: event.target.value })}/></label></div>}
      </fieldset>)}
      {value && <button className="secondary" disabled={busy || value.sources.length >= 30} onClick={() => setDraft({ ...value, sources: [...value.sources, { id: `custom-${randomId()}`, name: '自定义来源', kind: 'SKILL_GIT', url: '', ref: 'HEAD', repo_path: '', enabled: true, builtin: false }] })}><Plus size={14}/>添加自定义来源</button>}
      <p className="market-source-note">内置源可停用。自定义 Plugin 源使用兼容的插件市场清单；Skill 源支持公开 GitHub 仓库。分支和标签在读取时固定为具体提交，导入保留完整技能目录。</p>
      {error && <p className="plugin-import-error" role="alert">{error}</p>}
    </div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !value} onClick={() => void save()}>{busy ? '保存中…' : '保存来源'}</button></footer>
  </section></div>;
}
