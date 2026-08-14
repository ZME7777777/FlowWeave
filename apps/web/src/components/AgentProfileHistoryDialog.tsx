import { useQuery } from '@tanstack/react-query';
import { GitCompareArrows, ShieldCheck, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';

interface Props {
  packageId: string;
  capabilityKey: string;
  onClose: () => void;
}

function display(value: unknown): string {
  if (value === undefined) return '—';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function AgentProfileHistoryDialog({ packageId, capabilityKey, onClose }: Props) {
  const versions = useQuery({
    queryKey: ['agent-profile-versions', packageId],
    queryFn: () => api.agentProfileVersions(packageId),
  });
  const [selectedId, setSelectedId] = useState<string>();
  const selected = versions.data?.find(item => item.id === selectedId) ?? versions.data?.[0];
  const previous = versions.data?.find(item => item.version_no === (selected?.version_no ?? 0) - 1);
  const bindings = useQuery({
    queryKey: ['agent-profile-bindings', selected?.id],
    queryFn: () => api.agentProfileBindings(selected!.id),
    enabled: Boolean(selected?.id),
  });
  const changes = useMemo(() => {
    if (!selected) return [];
    const keys = new Set([...Object.keys(previous?.document ?? {}), ...Object.keys(selected.document)]);
    return [...keys].sort().filter(key => JSON.stringify(previous?.document[key]) !== JSON.stringify(selected.document[key]));
  }, [previous, selected]);

  return <div className="modal-backdrop"><section className="modal agent-profile-history" role="dialog" aria-modal="true" aria-label={`Agent Profile ${capabilityKey} 版本治理`}>
    <header><div><span className="eyebrow">IMMUTABLE AGENT PROFILE</span><h2>{capabilityKey}</h2></div><button className="ghost" onClick={onClose}><X size={14}/>关闭</button></header>
    <p>每个版本由固定 digest 标识。激活只允许通过新 Run Snapshot 与新 Attempt；既有执行不会热改。</p>
    {versions.isLoading ? <div className="empty compact">加载 Profile 版本…</div> : versions.error ? <p className="error">{versions.error.message}</p> : <div className="agent-profile-history-layout">
      <nav aria-label="Profile 版本">{versions.data?.map(item => <button key={item.id} className={selected?.id === item.id ? 'active' : ''} onClick={() => setSelectedId(item.id)}><b>Version {item.version_no}</b><small>{item.state} · {item.digest.slice(0, 12)}</small></button>)}</nav>
      {selected && <main><section className="agent-profile-provenance"><ShieldCheck size={16}/><div><b>OpenHands {selected.compatibility.openhands_version}</b><code>{selected.compatibility.source_commit}</code><span>{selected.compatibility.activation_semantics}</span></div></section>
        <h3><GitCompareArrows size={14}/>相对 Version {previous?.version_no ?? '起点'} 的字段差异</h3>
        {changes.length ? <dl className="agent-profile-diff">{changes.map(key => <div key={key}><dt>{key}</dt><dd><del>{display(previous?.document[key])}</del><ins>{display(selected.document[key])}</ins></dd></div>)}</dl> : <p className="field-hint">没有字段差异。</p>}
        <h3>当前节点绑定</h3>{bindings.isLoading ? <p className="field-hint">加载绑定…</p> : bindings.data?.length ? <div className="agent-profile-bindings">{bindings.data.map(item => <span key={item.node_asset_id}><b>{item.node_name}</b><small>position {item.position}</small></span>)}</div> : <p className="field-hint">此版本当前没有 Node Asset 绑定；历史 Snapshot 仍可继续引用。</p>}
      </main>}
    </div>}
  </section></div>;
}
