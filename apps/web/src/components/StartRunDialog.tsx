import { useMemo, useState } from 'react';
import type { ArtifactInput, FlowDefinition, NodeAsset } from '../types';
interface Props { flow: FlowDefinition; assets: NodeAsset[]; onStart: (input: { name?: string; flow_node_key: string; artifacts: ArtifactInput[] }) => Promise<void>; onClose: () => void }
export function StartRunDialog({ flow, assets, onStart, onClose }: Props) {
  const [entry, setEntry] = useState(flow.default_entry_key ?? flow.nodes[0]?.instance_key ?? '');
  const [name, setName] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const node = flow.nodes.find(item => item.instance_key === entry);
  const asset = useMemo(() => assets.find(item => item.id === node?.node_asset_id), [assets, node?.node_asset_id]);
  return <div className="modal-backdrop"><form className="modal start-run-modal" onSubmit={async e => { e.preventDefault(); setBusy(true); setError(''); try { await onStart({ name: name || undefined, flow_node_key: entry, artifacts: (asset?.inputs ?? []).filter(field => values[field.field_key]).map(field => ({ field_key: field.field_key, artifact_type: field.data_type, inline_content: values[field.field_key], mime_type: 'text/plain' })) }); } catch (reason) { setError(reason instanceof Error ? reason.message : '启动失败'); } finally { setBusy(false); } }}>
    <header><div><span className="eyebrow">START FLOW RUN</span><h2>启动流程 · {flow.name}</h2><p>可从任意流程节点开始；输入将登记为不可变人工产物。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <label>运行名称<input value={name} placeholder={`${flow.name} · 新运行`} onChange={e => setName(e.target.value)}/></label>
    <label>开始节点<select aria-label="开始节点" value={entry} onChange={e => { setEntry(e.target.value); setValues({}); }}><option value="">请选择</option>{flow.nodes.map(item => <option key={item.instance_key} value={item.instance_key}>{item.alias || assets.find(assetItem => assetItem.id === item.node_asset_id)?.name || item.instance_key}</option>)}</select></label>
    <section className="start-inputs"><h3>初始输入产物</h3>{asset?.inputs.length ? asset.inputs.map(field => <label key={field.field_key}>{field.display_name}<small>{field.field_key} · {field.data_type} · MVP 必填</small><textarea aria-label={field.display_name} required value={values[field.field_key] ?? ''} onChange={e => setValues({ ...values, [field.field_key]: e.target.value })}/></label>) : <div className="empty compact">该节点没有输入字段，可直接启动。</div>}</section>
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={!entry || busy}>{busy ? '创建中…' : '创建运行'}</button></footer>
  </form></div>;
}
