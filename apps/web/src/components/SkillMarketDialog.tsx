import { Check, LoaderCircle, Search, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { MarketSkill } from '../types';
import { MarketSourcePicker } from './MarketSources';
import { useMarketSources } from './useMarketSources';
import { useEscapeClose } from './useEscapeClose';
import './plugin-import.css';

const reasonMessage = (reason: unknown) => reason instanceof Error ? reason.message : '请求失败，请重试';
export function SkillMarketDialog({ onClose, onPublished }: { onClose: () => void; onPublished: () => void | Promise<void> }) {
  useEscapeClose(onClose);
  const market = useMarketSources('SKILL');
  const source = market.source;
  const [query, setQuery] = useState('');
  const [skills, setSkills] = useState<MarketSkill[]>([]);
  const [selected, setSelected] = useState(new Map<string, MarketSkill>());
  const [imported, setImported] = useState(new Set<string>());
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState('');
  const [searchError, setSearchError] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const tokens = useRef(new Map<string, string>());
  const epoch = useRef(0);
  const searchEpoch = useRef(0);
  const abort = useRef<AbortController | undefined>(undefined);
  const search = async () => {
    if (!source) return;
    const request = ++searchEpoch.current;
    abort.current?.abort(); abort.current = new AbortController();
    setLoading(true); setSearchError(''); setSkills([]);
    try {
      const result = await api.marketSkills(source.id, query, abort.current.signal);
      if (request !== searchEpoch.current) return;
      setSkills(result.skills); setSearched(!result.search_required);
    } catch (reason) { if (request === searchEpoch.current) setSearchError(reasonMessage(reason)); }
    finally { if (request === searchEpoch.current) setLoading(false); }
  };
  useEffect(() => {
    epoch.current += 1; searchEpoch.current += 1; abort.current?.abort();
    setSkills([]); setSelected(new Map()); setImported(new Set()); tokens.current.clear(); setQuery(''); setError(''); setSearchError(''); setStatus(''); setSearched(false); setLoading(false);
  }, [source]);
  useEffect(() => () => { epoch.current += 1; searchEpoch.current += 1; abort.current?.abort(); }, []);
  const importSelected = async () => {
    if (!source) return;
    const request = ++epoch.current;
    const current = () => request === epoch.current;
    setBusy(true); setError('');
    let completed = 0;
    const failures: string[] = [];
    try {
      for (const skill of selected.values()) {
        if (!current()) return;
        try {
          setStatus(`正在导入 ${skill.name}…`);
          let token = tokens.current.get(skill.id);
          if (!token) {
            const validation = await api.validateMarketSkill(source.id, skill);
            if (!current()) return;
            token = validation.import_token;
            if (!token) throw new Error('来源校验未返回导入凭证');
            tokens.current.set(skill.id, token);
          }
          await api.commitCapability(token);
          if (!current()) return;
          tokens.current.delete(skill.id); completed += 1;
          setImported(previous => new Set(previous).add(skill.id));
          setSelected(previous => { const next = new Map(previous); next.delete(skill.id); return next; });
        } catch (reason) { if (!current()) return; failures.push(`${skill.name}：${reasonMessage(reason)}`); tokens.current.delete(skill.id); }
      }
      if (completed) await onPublished();
      if (!current()) return;
      setStatus(completed ? `已导入 ${completed} 个 Skill` : '');
      if (failures.length) setError(failures.join('；'));
    } finally { if (current()) setBusy(false); }
  };
  return <div className="modal-backdrop"><section className="modal plugin-import-dialog" role="dialog" aria-modal="true" aria-label="Skill 市场">
    <header className="plugin-import-header"><h2>Skill 市场</h2><button className="ghost" aria-label="关闭 Skill 市场" onClick={onClose}><X size={18}/></button></header>
    <div className="plugin-import-body"><section className="plugin-import-market" aria-label="Skill 市场">
      <MarketSourcePicker market={market} disabled={busy}/>
      <form className="plugin-import-search" onSubmit={event => { event.preventDefault(); void search(); }}><label><Search size={14}/><input aria-label="搜索 Skill" disabled={busy} value={query} placeholder={source?.kind === 'SKILLS_SH' ? '输入至少两个字符，搜索 skills.sh…' : '搜索仓库中的 Skill，留空浏览全部…'} maxLength={200} onChange={event => setQuery(event.target.value)}/></label><button className="secondary" type="submit" disabled={!source || busy || loading || (source.kind === 'SKILLS_SH' && query.trim().length < 2)}>搜索</button></form>
      {loading ? <div className="plugin-import-empty" role="status"><LoaderCircle className="spin" size={20}/>正在搜索技能…</div> : searchError ? <div className="plugin-import-empty"><p role="alert">{searchError}</p><button className="secondary" onClick={() => void search()}>重新搜索</button></div> : <div className="plugin-import-list" aria-label="技能目录">{skills.map(skill => <button key={skill.id} role="checkbox" aria-label={`选择技能 ${skill.name}`} aria-checked={selected.has(skill.id)} disabled={busy || imported.has(skill.id)} onClick={() => setSelected(previous => { const next = new Map(previous); if (next.has(skill.id)) next.delete(skill.id); else next.set(skill.id, skill); return next; })}><span className="plugin-import-check">{selected.has(skill.id) && <Check size={12}/>}</span><Sparkles size={17}/><span className="plugin-import-copy"><b>{skill.name}</b><small title={skill.description}>{skill.description || skill.repository}</small></span><span className="plugin-import-version">{imported.has(skill.id) ? '已导入' : skill.installs ? `${skill.installs.toLocaleString()} 次安装` : skill.commit?.slice(0, 8)}</span></button>)}{!skills.length && <div className="plugin-import-empty"><Search size={21}/><span>{searched ? '没有匹配的技能' : '搜索并选择要导入的技能'}</span><small>{source?.kind === 'SKILLS_SH' ? '通过 skills.sh 公开搜索发现技能，导入时固定 Git 提交并保留完整目录。每次最多显示 50 条搜索结果。' : '可按名称和说明搜索，或留空搜索以浏览该仓库。'}</small></div>}</div>}
      {!!skills.length && <div className="plugin-import-pagination"><small>{source?.name} · {skills.length} 个结果 · 已选择 {selected.size} 个</small><button className="ghost" disabled={busy || !selected.size} onClick={() => setSelected(new Map())}>取消全部选择</button></div>}
    </section>{error && <p className="plugin-import-error" role="alert">{error}</p>}</div>
    <footer className="plugin-import-footer"><span role="status">{busy && <LoaderCircle className="spin" size={13}/>} {busy ? status : status || `已选择 ${selected.size} 个 Skill`}</span><button className="ghost" onClick={onClose}>{imported.size ? '完成' : '取消'}</button><button className="primary" disabled={busy || !selected.size || !source} onClick={() => void importSelected()}>{busy ? '导入中…' : `导入技能${selected.size ? ` (${selected.size})` : ''}`}</button></footer>
  </section></div>;
}
