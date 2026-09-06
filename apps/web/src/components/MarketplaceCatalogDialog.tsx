import { CheckCircle2, LoaderCircle, Search, ShieldCheck, Store, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import { Pagination } from './Pagination';
import { useEscapeClose } from './useEscapeClose';
import type { MarketplaceCatalog, PluginSourceResolution } from '../types';

const PAGE_SIZE = 6;

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
  const [page, setPage] = useState(1);
  const [resolution, setResolution] = useState<PluginSourceResolution>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pollGeneration = useRef(0);
  const visiblePlugins = useMemo(() => (catalog?.plugins ?? []).filter(plugin =>
    !query || `${plugin.name} ${plugin.description ?? ''} ${plugin.category ?? ''}`.toLowerCase().includes(query.toLowerCase()),
  ), [catalog, query]);
  const pagePlugins = useMemo(
    () => visiblePlugins.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [page, visiblePlugins],
  );

  useEffect(() => { setPage(1); }, [catalog?.commit, query]);

  const browse = async () => {
    setBusy(true); setError(''); setResolution(undefined);
    try {
      const result = await api.openhandsMarketplaceCatalog();
      setCatalog(result);
      setSelectedName(result.plugins[0]?.name ?? '');
      setPage(1);
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
    <header><h2>导入公开 Plugin</h2><button className="ghost" onClick={onClose}><X size={14}/>关闭</button></header>
    <section className="marketplace-catalog" aria-label="Plugin 目录">
      <div className="marketplace-toolbar">
        <label className="marketplace-search"><Search size={14}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder={`搜索 ${catalog?.plugins.length ?? 0} 个 Plugin`}/></label>
        <button className="secondary" disabled={busy} onClick={() => void browse()}>{busy && !catalog ? <LoaderCircle size={13}/> : <Store size={13}/>}刷新目录</button>
      </div>
      {catalog && <div className="marketplace-catalog-meta"><ShieldCheck size={13}/><span>{catalog.marketplace_name} · {catalog.plugins.length} 项 · {catalog.commit.slice(0, 12)}</span></div>}
      <div className="marketplace-plugin-list">{pagePlugins.map(plugin => <button key={plugin.name} className={selectedName === plugin.name ? 'selected' : ''} onClick={() => { setSelectedName(plugin.name); setResolution(undefined); }}><span><b>{plugin.name}</b><small>{plugin.description || '无说明'}</small></span><em>{plugin.version || plugin.category || '未声明版本'}</em></button>)}{catalog && !visiblePlugins.length && <div className="empty compact">没有匹配的 Plugin。</div>}</div>
      <Pagination page={page} pageSize={PAGE_SIZE} total={visiblePlugins.length} onPageChange={setPage}/>
    </section>
    <section className="marketplace-resolver" aria-label="Plugin 解析器">
      <header><div><span>解析器</span><b>{selectedName ? `已选择 ${selectedName}` : '请选择一个 Plugin'}</b></div><button className="secondary" disabled={busy || !selectedName} onClick={() => void resolve()}>{busy && resolution?.state === 'PENDING' ? '解析中…' : '解析条目来源'}</button></header>
      {resolution ? <section className={`marketplace-resolution ${resolution.state.toLowerCase()}`}><div><b>{resolution.state}</b><code>workflow v{resolution.state_version}</code></div><dl><dt>目录</dt><dd>{resolution.source_url}@{resolution.requested_commit}</dd><dt>来源</dt><dd>{resolution.resolved_source_url || '解析中'}{resolution.resolved_commit ? `@${resolution.resolved_commit}` : ''}</dd><dt>冻结</dt><dd>{resolution.content_hash || '尚未生成'}</dd></dl>{resolution.state === 'PUBLISHED' && <p><CheckCircle2 size={14}/>不可变 Capability Version 已发布。</p>}</section> : <p className="marketplace-resolver-empty">选择目录条目后，解析其固定来源并生成可发布的冻结内容。</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || resolution?.state !== 'READY'} onClick={() => void publish()}>{busy ? '处理中…' : '发布不可变 Version'}</button></footer>
  </section></div>;
}
