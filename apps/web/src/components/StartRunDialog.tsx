import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import type { FlowDefinition } from '../types';
import { useEscapeClose } from './useEscapeClose';

export interface RunStartInput {
  name?: string;
  environment_version_id: string;
}

interface Props {
  flow: FlowDefinition;
  onStart: (input: RunStartInput) => Promise<void>;
  onClose: () => void;
}

export function StartRunDialog({ flow, onStart, onClose }: Props) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [environmentVersionId, setEnvironmentVersionId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const environments = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments });
  const versions = useMemo(() => (environments.data ?? []).flatMap(environment =>
    environment.versions
      .filter(version => version.state === 'READY' && Boolean(version.image_digest))
      .map(version => ({ environment, version })),
  ), [environments.data]);

  return <div className="modal-backdrop"><form className="modal start-run-modal" onSubmit={async event => {
    event.preventDefault();
    if (!environmentVersionId) return;
    setBusy(true); setError('');
    try {
      await onStart({ name: name.trim() || undefined, environment_version_id: environmentVersionId });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建失败'); }
    finally { setBusy(false); }
  }}>
    <header><div><span className="eyebrow">CREATE FLOW RUN</span><h2>创建运行 · {flow.name}</h2><p>创建运行快照并固定本次运行使用的终端镜像。之后启动的节点执行和 Agent 对话都会使用该镜像。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <label>运行名称<input value={name} placeholder={`${flow.name} · 新运行`} onChange={event => setName(event.target.value)}/></label>
    <label>终端镜像<select required aria-label="终端镜像" value={environmentVersionId} onChange={event => setEnvironmentVersionId(event.target.value)}><option value="">请选择已发布镜像</option>{versions.map(({ environment, version }) => <option key={version.id} value={version.id}>{environment.name} · v{version.version_no}</option>)}</select><small>镜像版本在运行创建后保持不变。</small></label>
    {environments.isLoading && <div className="start-empty-run"><b>正在加载终端镜像…</b></div>}
    {!environments.isLoading && versions.length === 0 && <div className="start-empty-run warning"><b>没有可用的终端镜像</b><span>请先在“终端环境”中发布至少一个镜像版本，再创建运行。</span></div>}
    {environments.error && <p className="error">{environments.error.message}</p>}
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || environments.isLoading || !environmentVersionId}>{busy ? '创建中…' : '创建运行'}</button></footer>
  </form></div>;
}
