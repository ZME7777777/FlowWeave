import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Upload, X } from 'lucide-react';
import { api } from '../api/client';
import { LaunchOptionMenu } from './LaunchOptionMenu';
import { RecordConfigImportDialog } from '../pages/WorkbenchPage';
import { useWorkbenchStore } from '../store/workbench';
import { useEscapeClose } from './useEscapeClose';
import type { FlowRunAutomaticRecord, FlowRunStepwiseRecord, RecordConfigDocument } from '../types';

type RecordMode = 'MANUAL' | 'AUTOMATIC';

const recordModeOptions = [
  { value: 'MANUAL', label: '逐步运行' },
  { value: 'AUTOMATIC', label: '连续运行' },
] as const;

export function CreateRunRecordDialog({ runId, onClose }: { runId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<RecordMode>('MANUAL');
  const [name, setName] = useState('');
  const [importing, setImporting] = useState(false);
  const run = useQuery({ queryKey: ['flow-run', runId], queryFn: () => api.flowRun(runId) });
  const snapshot = run.data?.snapshots.find(item => item.id === run.data.active_snapshot_id) ?? run.data?.snapshots.at(-1);
  const nodes = snapshot?.definition.nodes ?? [];
  const [startNodeKey, setStartNodeKey] = useState('');
  const selectedStartNode = startNodeKey || nodes[0]?.instance_key || '';
  useEscapeClose(onClose, !importing);
  const created = (record: FlowRunAutomaticRecord | FlowRunStepwiseRecord, recordMode: RecordMode) => {
    const kind = recordMode === 'AUTOMATIC' ? 'automatic' : 'stepwise';
    qc.setQueryData([`flow-run-${kind}-record`, runId, record.id], record);
    void qc.invalidateQueries({ queryKey: ['flow-run-record-directory', runId] });
    void qc.invalidateQueries({ queryKey: [`flow-run-${kind}-records`, runId] });
    onClose();
    if (recordMode === 'AUTOMATIC') useWorkbenchStore.getState().openAutomaticRecord(runId, record.id);
    else useWorkbenchStore.getState().openStepwiseRecord(runId, record.id);
  };
  const create = useMutation({
    mutationFn: async () => {
      if (!run.data) throw new Error('正在读取 FlowRun。');
      if (mode === 'AUTOMATIC') {
        if (!run.data.environment_version_id) throw new Error('当前流程运行未绑定运行环境。');
        return { record: await api.createAutomaticRecord(runId, { name: name.trim() || undefined, environment_version_id: run.data.environment_version_id, start_node_key: selectedStartNode, node_plans: {} }), mode };
      }
      return { record: await api.createStepwiseRecord(runId, { name: name.trim() || undefined, start_node_key: selectedStartNode }), mode };
    },
    onSuccess: result => created(result.record, result.mode),
  });
  const importConfig = async (config: RecordConfigDocument) => {
    if (mode === 'AUTOMATIC') {
      if (config.format !== 'flowweave.continuous-record-config') throw new Error('这不是连续运行配置文件。');
      const records = await api.importAutomaticRecordConfigs(runId, config);
      records.forEach(record => qc.setQueryData(['flow-run-automatic-record', runId, record.id], record));
      void qc.invalidateQueries({ queryKey: ['flow-run-record-directory', runId] });
      void qc.invalidateQueries({ queryKey: ['flow-run-automatic-records', runId] });
      if (records[0]) created(records[0], mode);
    } else {
      if (config.format !== 'flowweave.stepwise-record-config') throw new Error('这不是逐步运行配置文件。');
      const records = await api.importStepwiseRecordConfigs(runId, config);
      records.forEach(record => qc.setQueryData(['flow-run-stepwise-record', runId, record.id], record));
      void qc.invalidateQueries({ queryKey: ['flow-run-record-directory', runId] });
      void qc.invalidateQueries({ queryKey: ['flow-run-stepwise-records', runId] });
      if (records[0]) created(records[0], mode);
    }
  };
  if (importing) return <RecordConfigImportDialog mode={mode === 'MANUAL' ? 'STEPWISE' : 'AUTOMATIC'} onClose={() => setImporting(false)} onImport={importConfig}/>;
  return <div className="modal-backdrop"><section className="modal automatic-record-dialog record-create-dialog" role="dialog" aria-modal="true" aria-label="新建运行记录"><header><div><span className="eyebrow">NEW RUN RECORD</span><h2>新建运行记录</h2><p>选择运行类型、名称和起始节点；Git 身份将自动冻结为当前用户身份。</p></div><button type="button" className="ghost" aria-label="关闭新建运行记录" onClick={onClose}><X size={16}/></button></header><label>运行类型<LaunchOptionMenu label="运行类型" value={mode} options={[...recordModeOptions]} onChange={value => setMode(value as RecordMode)}/></label><label>名称<input aria-label="运行记录名称" value={name} maxLength={220} autoFocus placeholder={`${run.data?.name ?? 'FlowRun'} · ${mode === 'AUTOMATIC' ? '连续运行' : '批次执行'}`} onChange={event => setName(event.target.value)}/></label><label>起始节点<LaunchOptionMenu label="运行记录起始节点" value={selectedStartNode} options={nodes.map(node => ({ value: node.instance_key, label: node.alias || node.asset.name }))} disabled={!nodes.length} onChange={setStartNodeKey}/></label>{run.isError && <p className="error">读取流程运行失败：{run.error.message}</p>}{create.error && <p className="error">创建失败：{create.error.message}</p>}<footer><button type="button" className="ghost" disabled={create.isPending} onClick={onClose}>取消</button><button type="button" className="secondary" disabled={create.isPending} onClick={() => setImporting(true)}><Upload size={13}/>导入 / 粘贴</button><button type="button" className="primary" disabled={!selectedStartNode || create.isPending || !run.data} onClick={() => create.mutate()}>{create.isPending ? '创建中…' : mode === 'AUTOMATIC' ? '创建草稿' : '创建记录'}</button></footer></section></div>;
}
