import { X } from 'lucide-react';
import { useState } from 'react';
import type { FlowDefinition } from '../types';
import { useEscapeClose } from './useEscapeClose';

export interface RunStartInput {
  name?: string;
}

interface Props {
  flow: FlowDefinition;
  onStart: (input: RunStartInput) => Promise<void>;
  onClose: () => void;
}

export function StartRunDialog({ flow, onStart, onClose }: Props) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <div className="modal-backdrop"><form className="modal start-run-modal" onSubmit={async event => {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      await onStart({ name: name.trim() || undefined });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建失败'); }
    finally { setBusy(false); }
  }}>
    <header><div><span className="eyebrow">CREATE FLOW RUN</span><h2>创建运行 · {flow.name}</h2><p>运行将冻结流程已绑定的 Environment Version；运行期间不能切换。</p></div><button type="button" className="start-run-close" aria-label="关闭创建运行弹窗" title="关闭" onClick={onClose}><X size={18}/></button></header>
    <label>运行名称<input value={name} placeholder={`${flow.name} · 新运行`} onChange={event => setName(event.target.value)}/></label>
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy}>{busy ? '创建中…' : '创建运行'}</button></footer>
  </form></div>;
}
