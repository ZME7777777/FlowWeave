import { Check, ChevronRight, GitBranch, LoaderCircle, Package, RefreshCw, Search, Store, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { MarketSourcePicker } from './MarketSources';
import { useMarketSources } from './useMarketSources';
import { Pagination } from './Pagination';
import { useEscapeClose } from './useEscapeClose';
import type { MarketplaceCatalog, PluginSourceResolution } from '../types';
import './plugin-import.css';

const PAGE_SIZE = 10;
type Source = 'marketplace' | 'git';
interface Props {
  onClose: () => void;
  onPublished: () => void | Promise<void>;
}
function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : '导入失败，请重试。';
}

function gitSource(value: string, commit: string, repoPath: string) {
  let url: URL;
  try { url = new URL(value.trim()); } catch { throw new Error('请输入 HTTPS 仓库地址或 GitHub 固定提交链接。'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    throw new Error('请使用不含账号、密码或查询参数的 HTTPS 链接。');
  }
  const segments = url.pathname.replace(/\/$/u, '').split('/').filter(Boolean);
  let revision = commit.trim().toLowerCase();
  let path = repoPath.trim();
  if (url.hostname === 'github.com' && ['commit', 'tree'].includes(segments[2])) {
    if (!/^[0-9a-f]{40}$/iu.test(segments[3] ?? '')) throw new Error('请打开具体提交后复制链接；分支链接不能固定版本。');
    revision = segments[3].toLowerCase();
    if (segments[2] === 'tree') path = segments.slice(4).join('/');
    url.pathname = `/${segments[0]}/${segments[1]}`;
  }
  if (!/^[0-9a-f]{40}$/u.test(revision)) throw new Error('请填写完整的 40 位提交号，或直接粘贴 GitHub 固定提交链接。');
  if (path && (!/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/u.test(path) || path.split('/').some(part => part === '.' || part === '..'))) {
    throw new Error('插件目录请填写仓库内的相对路径，例如 plugins/review。');
  }
  return { source_url: url.toString().replace(/\/$/u, ''), commit: revision, repo_path: path || null };
}

export function PluginMarketDialog({ onClose, onPublished }: Props) {
  useEscapeClose(onClose);
  const market = useMarketSources('PLUGIN');
  const marketSource = market.source;
  const [source, setSource] = useState<Source>('marketplace');
  const [catalog, setCatalog] = useState<MarketplaceCatalog>();
  const [loading, setLoading] = useState(true);
  const [catalogError, setCatalogError] = useState('');
  const [selectedNames, setSelectedNames] = useState<Set<string>>(new Set());
  const [importedNames, setImportedNames] = useState<Set<string>>(new Set());
  const [publishedCount, setPublishedCount] = useState(0);
  const marketplaceResolutions = useRef(new Map<string, PluginSourceResolution>());
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const queryRef = useRef(query); queryRef.current = query;
  const [url, setUrl] = useState('');
  const [commit, setCommit] = useState('');
  const [repoPath, setRepoPath] = useState('');
  const [resolution, setResolution] = useState<PluginSourceResolution>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const generation = useRef(0);
  const browseGeneration = useRef(0);
  const browseAbort = useRef<AbortController | undefined>(undefined);
  const pinnedCommit = useRef<string | undefined>(undefined);
  const [searchTerm, setSearchTerm] = useState('');
  const [reload, setReload] = useState(0);
  const plugins = catalog?.plugins ?? [];
  useEffect(() => {
    const timer = window.setTimeout(() => setSearchTerm(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    generation.current += 1; browseGeneration.current += 1; browseAbort.current?.abort();
    pinnedCommit.current = undefined;
    setCatalog(undefined); setSelectedNames(new Set()); setImportedNames(new Set());
    marketplaceResolutions.current.clear(); setPublishedCount(0); setError(''); setStatus('');
    setQuery(''); setSearchTerm(''); setPage(1);
  }, [marketSource]);
  const browse = useCallback(async () => {
    if (!marketSource) return;
    const request = ++browseGeneration.current;
    browseAbort.current?.abort();
    const controller = new AbortController(); browseAbort.current = controller;
    setLoading(true); setCatalogError(''); setCatalog(undefined);
    try {
      const result = await api.marketPluginCatalog(marketSource.id, searchTerm, page, pinnedCommit.current, controller.signal);
      if (request !== browseGeneration.current || searchTerm !== queryRef.current.trim()) return;
      pinnedCommit.current = result.commit; setCatalog(result);
    } catch (reason) {
      if (request === browseGeneration.current && searchTerm === queryRef.current.trim()) setCatalogError(message(reason));
    } finally { if (request === browseGeneration.current && searchTerm === queryRef.current.trim()) setLoading(false); }
  }, [marketSource, searchTerm, page]);
  useEffect(() => {
    if (marketSource && source === 'marketplace') void browse();
    else if (!marketSource) { setCatalog(undefined); setLoading(market.isPending); }
    return () => { browseGeneration.current += 1; browseAbort.current?.abort(); };
  }, [browse, marketSource, market.isPending, source, reload]);
  useEffect(() => () => { generation.current += 1; }, []);
  const refreshCatalog = () => {
    generation.current += 1; pinnedCommit.current = undefined;
    setSelectedNames(new Set()); setImportedNames(new Set()); marketplaceResolutions.current.clear();
    setPublishedCount(0); setPage(1); setReload(value => value + 1);
  };

  const changeQuery = (value: string) => {
    setQuery(value); setPage(1);
    if (value.trim() !== searchTerm || page !== 1) { setCatalog(undefined); setLoading(true); }
  };
  const reset = () => { generation.current += 1; setResolution(undefined); setError(''); setStatus(''); setPublishedCount(0); };
  const importPlugin = async () => {
    const request = ++generation.current;
    setBusy(true); setError(''); setPublishedCount(0);
    const publish = async (initial: PluginSourceResolution | undefined, create: () => Promise<PluginSourceResolution>, update: (value: PluginSourceResolution) => void, label: string) => {
      setStatus(`正在读取${label}…`);
      let current = initial;
      if (!current || current.state === 'FAILED' || current.state === 'EXPIRED') current = await create();
      if (request !== generation.current) return;
      update(current);
      const deadline = Date.now() + 5 * 60_000;
      while (current.state === 'PENDING' && Date.now() < deadline) {
        setStatus(`正在检查${label}…`);
        await new Promise(resolve => window.setTimeout(resolve, 1500));
        if (request !== generation.current) return;
        current = await api.pluginSourceResolution(current.id);
        if (request !== generation.current) return;
        update(current);
      }
      if (current.state === 'FAILED') throw new Error(current.error_detail || '无法读取插件内容，请检查来源后重试。');
      if (current.state === 'EXPIRED') throw new Error('导入准备已过期，请重试。');
      if (current.state === 'PENDING') throw new Error('插件检查仍在进行，请稍后重试以继续查看结果。');
      if (current.state === 'READY') {
        setStatus(`正在导入${label}…`);
        current = await api.publishPluginSourceResolution(current.id, current.state_version);
        if (request !== generation.current) return;
        update(current);
      }
      return current;
    };
    try {
      if (source === 'marketplace') {
        if (!catalog || !selectedNames.size) throw new Error('请先选择插件。');
        const names = [...selectedNames];
        const failures: string[] = [];
        for (const [index, name] of names.entries()) {
          try {
            const current = await publish(marketplaceResolutions.current.get(name), () => api.createMarketplacePluginResolution({
              marketplace_source_url: catalog.source, marketplace_commit: catalog.commit,
              marketplace_repo_path: catalog.repo_path, plugin_name: name,
            }), value => marketplaceResolutions.current.set(name, value), ` ${name}（${index + 1}/${names.length}）`);
            if (!current || request !== generation.current) return;
            if (current.state === 'PUBLISHED') {
              setImportedNames(previous => new Set(previous).add(name));
              setSelectedNames(previous => { const next = new Set(previous); next.delete(name); return next; });
              setPublishedCount(previous => previous + 1);
              await onPublished();
              if (request !== generation.current) return;
            }
          } catch (reason) {
            if (request !== generation.current) return;
            failures.push(`${name}：${message(reason)}`);
          }
        }
        setError(failures.join('；'));
      } else {
        const current = await publish(resolution, () => {
          const input = gitSource(url, commit, repoPath);
          setCommit(input.commit); setRepoPath(input.repo_path ?? ''); setUrl(input.source_url);
          return api.createPluginSourceResolution(input);
        }, setResolution, '插件');
        if (current?.state === 'PUBLISHED' && request === generation.current) await onPublished();
      }
    } catch (reason) { if (request === generation.current) setError(message(reason)); }
    finally { if (request === generation.current) { setBusy(false); setStatus(''); } }
  };

  return <div className="modal-backdrop"><section className="modal plugin-import-dialog" role="dialog" aria-modal="true" aria-label="插件市场">
    <header className="plugin-import-header"><h2>插件市场</h2><button className="ghost" aria-label="关闭" onClick={onClose}><X size={16}/></button></header>
    <div className="plugin-import-sources" role="tablist" aria-label="导入来源">
      <button role="tab" aria-selected={source === 'marketplace'} disabled={busy} onClick={() => { reset(); setSource('marketplace'); }}><Store size={14}/>插件市场</button>
      <button role="tab" aria-selected={source === 'git'} disabled={busy} onClick={() => { reset(); setSource('git'); }}><GitBranch size={14}/>Git 仓库</button>
    </div>
    <div className="plugin-import-body">
      {source === 'marketplace' ? <section aria-label="插件市场" className="plugin-import-market">
        <MarketSourcePicker market={market} disabled={busy}/><div className="plugin-import-search"><label><Search size={14}/><input aria-label="搜索插件" placeholder="搜索插件名称或说明" value={query} disabled={busy} onChange={event => changeQuery(event.target.value)}/></label><button className="ghost" title="刷新目录" aria-label="刷新目录" disabled={loading || busy} onClick={refreshCatalog}><RefreshCw size={14}/></button></div>
        {loading ? <div className="plugin-import-empty" role="status"><LoaderCircle className="spin" size={20}/><span>正在加载插件目录…</span><small>正在读取当前页</small></div>
          : catalogError ? <div className="plugin-import-empty"><Store size={22}/><span>暂时无法加载插件市场</span><small role="alert">{catalogError}</small><button className="secondary" onClick={() => void browse()}>重新加载</button><button className="ghost" onClick={() => { reset(); setSource('git'); }}>从 Git 仓库导入<ChevronRight size={13}/></button></div>
          : <><div className="plugin-import-list" aria-label="插件目录">{plugins.map(plugin => <button key={plugin.name} role="checkbox" aria-label={`选择插件 ${plugin.name}`} disabled={busy || importedNames.has(plugin.name)} aria-checked={selectedNames.has(plugin.name)} onClick={() => { reset(); setSelectedNames(previous => { const next = new Set(previous); if (next.has(plugin.name)) next.delete(plugin.name); else next.add(plugin.name); return next; }); }}>
            <span className="plugin-import-check">{selectedNames.has(plugin.name) && <Check size={12}/>}</span><Package size={17}/><span className="plugin-import-copy"><b>{plugin.name}</b><small>{plugin.description || '暂无说明'}</small></span><span className="plugin-import-version">{importedNames.has(plugin.name) ? '已导入' : plugin.version || plugin.category}</span>
          </button>)}{!plugins.length && <div className="plugin-import-empty"><Search size={20}/><span>{query ? '没有匹配的插件' : '目录中暂时没有插件'}</span>{query && <button className="ghost" onClick={() => changeQuery('')}>清除搜索</button>}</div>}</div>
          <div className="plugin-import-pagination"><small>{marketSource?.name ?? '市场'} · {catalog?.total ?? plugins.length} 个插件</small><Pagination page={page} pageSize={PAGE_SIZE} total={catalog?.total ?? plugins.length} onPageChange={value => { if (!loading && !busy) setPage(value); }}/></div></>}
      </section> : <section className="plugin-import-git" aria-label="Git 仓库导入">
        <label>仓库或提交链接<input autoFocus value={url} disabled={busy} placeholder="https://github.com/owner/repo" onChange={event => { reset(); setUrl(event.target.value); }}/></label>
        <p>可直接粘贴 GitHub 的固定提交或目录链接，自动识别版本与插件目录。</p>
        <div className="plugin-import-fields"><label>完整提交号<input value={commit} disabled={busy} placeholder="40 位 commit SHA；粘贴提交链接时自动识别" onChange={event => { reset(); setCommit(event.target.value); }}/></label><label>插件目录 <small>可选</small><input value={repoPath} disabled={busy} placeholder="仓库根目录，或 plugins/review" onChange={event => { reset(); setRepoPath(event.target.value); }}/></label></div>
        <details className="plugin-import-help"><summary>如何获取导入链接？</summary><ol><li>打开公开仓库的提交记录，进入要导入的那次提交。</li><li>复制地址栏中的 <code>/commit/完整提交号</code> 链接，粘贴到上方。</li><li>如果插件在子目录中，填写包含插件清单的目录，如 <code>plugins/review</code>。</li></ol><p>支持含 <code>plugin.json</code>、<code>.plugin/plugin.json</code> 或 <code>.claude-plugin/plugin.json</code> 的插件目录。只有 SKILL.md 的技能请到 Skill 模块导入。</p><p>使用固定提交可确保导入版本不随仓库后续更新而改变。私有仓库和未获允许的来源会被拒绝。</p></details>
      </section>}
      {error && <p className="plugin-import-error" role="alert">{error}</p>}
      {(resolution?.state === 'PUBLISHED' || publishedCount > 0) && <p className="plugin-import-success" role="status"><Check size={14}/>{publishedCount > 1 ? `${publishedCount} 个插件已导入能力仓库。` : '插件已导入能力仓库。'}</p>}
    </div>
    <footer className="plugin-import-footer"><span>{busy ? <><LoaderCircle className="spin" size={13}/>{status}</> : source === 'marketplace' && selectedNames.size ? `已选择 ${selectedNames.size} 个插件` : '导入后可在会话配置中选择使用'}</span><button className="ghost" onClick={onClose}>{resolution?.state === 'PUBLISHED' || (source === 'marketplace' && importedNames.size > 0) ? '完成' : '取消'}</button><button className="primary" disabled={busy || resolution?.state === 'PUBLISHED' || (source === 'marketplace' ? loading || !!catalogError || !selectedNames.size : !url.trim())} onClick={() => void importPlugin()}>{busy ? '导入中…' : `${error ? '重试导入' : '导入插件'}${source === 'marketplace' && selectedNames.size ? ` (${selectedNames.size})` : ''}`}</button></footer>
  </section></div>;
}
