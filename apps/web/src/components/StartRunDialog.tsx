import { X } from 'lucide-react';
import { useState } from 'react';
import type { FlowDefinition, TerminalEnvironment } from '../types';
import { useEscapeClose } from './useEscapeClose';

export interface RunStartInput {
  name?: string;
  environment_version_id: string;
}

interface Props {
  flow: FlowDefinition;
  environments: TerminalEnvironment[];
  onStart: (input: RunStartInput) => Promise<void>;
  onClose: () => void;
}

export function StartRunDialog({ flow, environments, onStart, onClose }: Props) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [environmentVersionId, setEnvironmentVersionId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <div className="modal-backdrop"><form className="modal start-run-modal" onSubmit={async event => {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      await onStart({ name: name.trim() || undefined, environment_version_id: environmentVersionId });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建失败'); }
    finally { setBusy(false); }
  }}>
    <header><div><span className="eyebrow">CREATE FLOW RUN</span><h2>创建运行 · {flow.name}</h2><p>为本次运行选择基础镜像；同一流程模板的不同运行可以选择不同环境。</p></div><button type="button" className="start-run-close" aria-label="关闭创建运行弹窗" title="关闭" onClick={onClose}><X size={18}/></button></header>
    <label>运行名称<input value={name} placeholder={`${flow.name} · 新运行`} onChange={event => setName(event.target.value)}/></label>
    <label>运行环境（基础镜像）<select required aria-label="本次运行环境版本" value={environmentVersionId} onChange={event => setEnvironmentVersionId(event.target.value)}><option value="">请选择运行环境</option>{environments.flatMap(environment => environment.versions.filter(version => version.state === 'READY' && version.runtime_compatible && Boolean(version.image_digest)).map(version => <option key={version.id} value={version.id}>{environment.name} · v{version.version_no}</option>))}</select></label>
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !environmentVersionId}>{busy ? '正在创建运行…' : '启动流程'}</button></footer>
  </form></div>;
}
