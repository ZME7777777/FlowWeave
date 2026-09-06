import { CheckCircle2, LoaderCircle, Search, ShieldCheck, Store, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import { useEscapeClose } from './useEscapeClose';
import type { MarketplaceCatalog, PluginSourceResolution } from '../types';

interface Props {
  onClose: () => void;
  onPublished: () => void | Promise<void>;
}

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Marketplace 操作失败。';
}

export function MarketplaceCatalogDialog({ onClose, onPublished }: Props) {
  useEscapeClose(onClose);
  const [catalog, setCatalog] = useState<MarketplaceCatalog>();
  const [selectedName, setSelectedName] = useState('');
  const [query, setQuery] = useState('');
  const [resolution, setResolution] = useState<PluginSourceResolution>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pollGeneration = useRef(0);
  const visiblePlugins = useMemo(() => (catalog?.plugins ?? []).filter(plugin =>
    !query || `${plugin.name} ${plugin.description ?? ''} ${plugin.category ?? ''}`.toLowerCase().includes(query.toLowerCase()),
  ), [catalog, query]);

  const browse = async () => {
    setBusy(true); setError(''); setResolution(undefined);
    try {
      const result = await api.openhandsMarketplaceCatalog();
      setCatalog(result);
      setSelectedName(result.plugins[0]?.name ?? '');
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  };

  useEffect(() => { void browse(); }, []);

  const poll = async (initial: PluginSourceResolution, generation: number) => {
    let current = initial;
    const deadline = Date.now() + 5 * 60_000;
    while (current.state === 'PENDING' && Date.now() < deadline && pollGeneration.current === generation) {
      await new Promise(resolve => window.setTimeout(resolve, 1500));
      if (pollGeneration.current !== generation) return current;
      current = await api.pluginSourceResolution(current.id);
      setResolution(current);
    }
    return current;
  };

  const resolve = async () => {
    if (!selectedName || !catalog) return;
    setBusy(true); setError('');
    try {
      const generation = ++pollGeneration.current;
      const current = await api.createMarketplacePluginResolution({
        marketplace_source_url: catalog.source,
        marketplace_commit: catalog.commit,
        marketplace_repo_path: catalog.repo_path,
        plugin_name: selectedName,
      });
      setResolution(current);
      const completed = await poll(current, generation);
      if (completed.state === 'FAILED') setError(completed.error_detail || 'Plugin 解析失败。');
      if (completed.state === 'EXPIRED') setError('解析结果已过期，请重新解析固定快照。');
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  };

  const publish = async () => {
    if (!resolution || resolution.state !== 'READY') return;
    setBusy(true); setError('');
    try {
      const published = await api.publishPluginSourceResolution(resolution.id, resolution.state_version);
      setResolution(published);
      await onPublished();
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  };

  return <div className="modal-backdrop"><section className="modal marketplace-dialog" role="dialog" aria-modal="true" aria-label="浏览 OpenHands Marketplace">
    <header><div><span className="eyebrow">OPENHANDS MARKETPLACE</span><h2>导入公开 Plugin</h2></div><button className="ghost" onClick={onClose}><X size={14}/>关闭</button></header>
    <p>自动读取公开的 OpenHands/extensions Marketplace。每次刷新先解析最新 HEAD，再用返回的完整 commit 浏览、解析和冻结；运行时不会再访问远端仓库。</p>
    <div className="marketplace-selection"><div><b>OpenHands 官方公开 Marketplace</b><span>导入仍需显式发布，且固定到本次目录快照。</span></div><button className="secondary" disabled={busy} onClick={() => void browse()}>{busy && !catalog ? <LoaderCircle size={13}/> : <Store size={13}/>}刷新目录</button></div>
    {catalog && <><section className="marketplace-provenance"><ShieldCheck size={17}/><div><b>{catalog.marketplace_name}</b><span>{catalog.description || '无目录说明'} · owner {catalog.owner}</span><code>{catalog.source}@{catalog.commit}</code>{catalog.repo_path && <code>path: {catalog.repo_path}</code>}</div></section><label className="marketplace-search"><Search size={14}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder={`搜索 ${catalog.plugins.length} 个 Plugin`}/></label><div className="marketplace-plugin-list">{visiblePlugins.map(plugin => <button key={plugin.name} className={selectedName === plugin.name ? 'selected' : ''} onClick={() => { setSelectedName(plugin.name); setResolution(undefined); }}><span><b>{plugin.name}</b><small>{plugin.description || '无说明'}</small></span><em>{plugin.version || plugin.category || '未声明版本'}</em></button>)}{!visiblePlugins.length && <div className="empty compact">没有匹配的 Plugin。</div>}</div></>}
    {selectedName && <section className="marketplace-selection"><div><b>已选择 {selectedName}</b><span>先解析条目真实来源，再显式发布不可变 Version。</span></div><button className="secondary" disabled={busy} onClick={() => void resolve()}>{busy && resolution?.state === 'PENDING' ? '解析中…' : '解析条目来源'}</button></section>}
    {resolution && <section className={`marketplace-resolution ${resolution.state.toLowerCase()}`}><header><b>{resolution.state}</b><code>workflow v{resolution.state_version}</code></header><dl><dt>目录来源</dt><dd>{resolution.source_url}@{resolution.requested_commit}</dd><dt>条目</dt><dd>{resolution.marketplace_plugin_name}</dd><dt>实际 Plugin 来源</dt><dd>{resolution.resolved_source_url || '解析中'}{resolution.resolved_commit ? `@${resolution.resolved_commit}` : ''}</dd><dt>冻结内容</dt><dd>{resolution.content_hash || '尚未生成'}</dd></dl>{resolution.state === 'PUBLISHED' && <p><CheckCircle2 size={14}/>不可变 Capability Version 已发布。</p>}</section>}
    {error && <p className="error" role="alert">{error}</p>}
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || resolution?.state !== 'READY'} onClick={() => void publish()}>{busy ? '处理中…' : '发布不可变 Version'}</button></footer>
  </section></div>;
}
