import { useEffect, useMemo, useState } from 'react';
import type { CapabilityAsset, SkillCollection, SkillCollectionWrite } from '../types';
import { useEscapeClose } from './useEscapeClose';

interface Props {
  collection?: SkillCollection;
  skills: CapabilityAsset[];
  busy: boolean;
  onClose: () => void;
  onSave: (payload: SkillCollectionWrite) => void;
}

export function SkillCollectionEditorDialog({ collection, skills, busy, onClose, onSave }: Props) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [category, setCategory] = useState('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');

  useEffect(() => {
    setName(collection?.name ?? '');
    setCategory(collection?.category ?? '');
    setDescription(collection?.description ?? '');
    setSelected(new Set(collection?.members.map(item => item.id) ?? []));
  }, [collection]);

  const available = useMemo(() => {
    const memberIds = new Set(collection?.members.map(item => item.id) ?? []);
    return skills
      .filter(item => item.capability_type === 'SKILL' && (item.is_latest || memberIds.has(item.id)))
      .filter(item => !search || `${item.capability_key} ${item.description} ${item.version}`.toLowerCase().includes(search.toLowerCase()))
      .sort((left, right) => left.capability_key.localeCompare(right.capability_key));
  }, [collection, search, skills]);
  const toggle = (asset: CapabilityAsset) => setSelected(old => {
    const next = new Set(old);
    if (next.has(asset.id)) {
      next.delete(asset.id);
    } else {
      skills
        .filter(item => item.capability_type === 'SKILL' && item.capability_key === asset.capability_key)
        .forEach(item => next.delete(item.id));
      next.add(asset.id);
    }
    return next;
  });

  return <div className="modal-backdrop"><form className="modal skill-collection-editor" onSubmit={event => {
    event.preventDefault();
    onSave({
      name: name.trim(), category: category.trim(), description: description.trim(),
      capability_ids: [...selected], row_version: collection?.row_version ?? null,
    });
  }}>
    <header><div><span className="eyebrow">SKILL COLLECTION</span><h2>{collection ? '编辑 Skill 组合' : '新建 Skill 组合'}</h2></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <p>组合只是批量选择模板。节点添加组合后，仍会逐项保存下方真实 Skill 的固定版本引用。</p>
    <div className="skill-collection-fields">
      <label><span>组合名称 *</span><input required maxLength={200} value={name} onChange={event => setName(event.target.value)} placeholder="例如 产品需求分析"/></label>
      <label><span>分类</span><input maxLength={120} value={category} onChange={event => setCategory(event.target.value)} placeholder="例如 产品、研发、测试"/></label>
      <label className="wide"><span>说明</span><textarea maxLength={2000} value={description} onChange={event => setDescription(event.target.value)} placeholder="描述适用场景"/></label>
    </div>
    <section className="skill-collection-members"><header><div><b>固定 Skill 版本</b><span>已选择 {selected.size} 项</span></div><input aria-label="搜索组合 Skill" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索 Skill"/></header>
      <div>{available.map(item => <label key={item.id} className={selected.has(item.id) ? 'selected' : ''}><input type="checkbox" checked={selected.has(item.id)} onChange={() => toggle(item)}/><span><b>{item.capability_key}</b><small>{item.description || item.filename}</small></span><em>rev {item.revision_number}{item.version ? ` · ${item.version}` : ''}</em></label>)}{!available.length && <p>没有可选的 Skill。</p>}</div>
    </section>
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !name.trim() || selected.size === 0}>{busy ? '保存中…' : '保存组合'}</button></footer>
  </form></div>;
}
