import { useEffect, useMemo, useState } from 'react';
import type { CapabilityAsset, CapabilityCollection, CapabilityCollectionWrite } from '../types';
import { useEscapeClose } from './useEscapeClose';
import './capability-collection-editor.css';

interface Props {
  collection?: CapabilityCollection;
  capabilities: CapabilityAsset[];
  busy: boolean;
  onClose: () => void;
  onSave: (payload: CapabilityCollectionWrite) => void;
}

export function CapabilityCollectionEditorDialog({ collection, capabilities, busy, onClose, onSave }: Props) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const latestSkillsByLineage = useMemo(() => new Map(
    capabilities
      .filter(item => item.capability_type === 'SKILL' && item.is_latest)
      .map(item => [item.lineage_id, item]),
  ), [capabilities]);
  const latestSkillIdByVersionId = useMemo(() => new Map(
    capabilities
      .filter(item => item.capability_type === 'SKILL')
      .map(item => [item.id, latestSkillsByLineage.get(item.lineage_id)?.id ?? item.id]),
  ), [capabilities, latestSkillsByLineage]);

  useEffect(() => {
    setName(collection?.name ?? '');
    setDescription(collection?.description ?? '');
    setSelected(new Set(
      collection?.members.map(item => latestSkillIdByVersionId.get(item.id) ?? item.id) ?? [],
    ));
  }, [collection, latestSkillIdByVersionId]);

  const available = useMemo(() => {
    return capabilities
      .filter(item => item.capability_type === 'SKILL')
      .filter(item => item.is_latest)
      .filter(item => !search || `${item.capability_key} ${item.description} ${item.version}`.toLowerCase().includes(search.toLowerCase()))
      .sort((left, right) => left.capability_key.localeCompare(right.capability_key));
  }, [capabilities, search]);
  const toggle = (asset: CapabilityAsset) => setSelected(old => {
    const next = new Set(old);
    if (next.has(asset.id)) {
      next.delete(asset.id);
    } else {
      capabilities
        .filter(item => item.capability_type === asset.capability_type && item.capability_key === asset.capability_key)
        .forEach(item => next.delete(item.id));
      next.add(asset.id);
    }
    return next;
  });

  return <div className="modal-backdrop"><form className="modal capability-collection-editor" onSubmit={event => {
    event.preventDefault();
    onSave({
      name: name.trim(), category: collection?.category ?? '', description: description.trim(),
      capability_ids: [...selected], row_version: collection?.row_version ?? null,
    });
  }}>
    <header><h2>{collection ? '编辑 Skill 组合' : '新建 Skill 组合'}</h2><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="capability-collection-fields">
      <label><span>组合名称 *</span><input required maxLength={200} value={name} onChange={event => setName(event.target.value)} placeholder="例如 产品需求分析"/></label>
      <label><span>简短说明</span><input maxLength={2000} value={description} onChange={event => setDescription(event.target.value)} placeholder="一句话描述适用场景"/></label>
    </div>
    <section className="capability-collection-members"><header><div><b title="使用每个 Skill 的最新发布版本">Skill 列表</b><span>已选择 {selected.size} / {latestSkillsByLineage.size} 项</span></div><input aria-label="搜索组合 Skill" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索 Skill 名称或说明"/></header>
      <div>{available.map(item => <label key={item.id} className={selected.has(item.id) ? 'selected' : ''}><input type="checkbox" checked={selected.has(item.id)} onChange={() => toggle(item)}/><span><b title={item.capability_key}>{item.capability_key}</b><small title={item.description || item.filename}>{item.description || item.filename}</small></span><em>rev {item.revision_number}{item.version ? ` · ${item.version}` : ''}</em></label>)}{!available.length && <p>{capabilities.some(item => item.capability_type === 'SKILL') ? '没有匹配的 Skill 版本，请清空搜索条件后重试。' : '还没有可选的 Skill 版本，请先关闭窗口并上传 Skill ZIP。'}</p>}</div>
    </section>
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !name.trim() || selected.size === 0}>{busy ? '保存中…' : '保存组合'}</button></footer>
  </form></div>;
}
