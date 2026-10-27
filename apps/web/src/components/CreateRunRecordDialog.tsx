import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronDown, X } from 'lucide-react';
import { api } from '../api/client';
import { AutomaticRecordDialog, RecordConfigImportDialog, StepwiseRecordDialog } from '../pages/WorkbenchPage';
import { useWorkbenchStore } from '../store/workbench';
import { useEscapeClose } from './useEscapeClose';
import type { FlowRunAutomaticRecord, FlowRunStepwiseRecord, RecordConfigDocument } from '../types';

type RecordMode = 'MANUAL' | 'AUTOMATIC';

const recordModeOptions: Array<{ value: RecordMode; label: string; detail: string }> = [
  { value: 'MANUAL', label: '逐步运行', detail: '按节点配置、确认和启动。' },
  { value: 'AUTOMATIC', label: '连续运行', detail: '补齐节点计划后，按流程自动推进。' },
];

function RecordModeSelect({ value, onChange }: { value: RecordMode; onChange: (mode: RecordMode) => void }) {
  const id = useId();
  const host = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const selected = recordModeOptions.find(option => option.value === value)!;
  useEscapeClose(() => setOpen(false), open);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (event.target instanceof Node && !host.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  return <div className="record-mode-select" ref={host}>
    <label htmlFor={`${id}-trigger`}>运行类型</label>
    <button id={`${id}-trigger`} type="button" aria-label="运行类型" aria-haspopup="listbox" aria-expanded={open} aria-controls={`${id}-options`} onClick={() => setOpen(current => !current)}>
      <span><b>{selected.label}</b><small>{selected.detail}</small></span><ChevronDown size={16} aria-hidden="true"/>
    </button>
    {open && <div id={`${id}-options`} className="record-mode-options" role="listbox" aria-label="运行类型">
      {recordModeOptions.map(option => <button key={option.value} type="button" role="option" aria-selected={option.value === value} onClick={() => { onChange(option.value); setOpen(false); }}>
        <span><b>{option.label}</b><small>{option.detail}</small></span>{option.value === value && <Check size={16} aria-hidden="true"/>}
      </button>)}
    </div>}
  </div>;
}

export function CreateRunRecordDialog({ runId, onClose }: { runId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<RecordMode>('MANUAL');
  const [step, setStep] = useState<'MODE' | 'CREATE' | 'IMPORT'>('MODE');
  const run = useQuery({ queryKey: ['flow-run', runId], queryFn: () => api.flowRun(runId) });
  useEscapeClose(onClose, step === 'MODE');
  const created = (record: FlowRunAutomaticRecord | FlowRunStepwiseRecord) => {
    const kind = mode === 'AUTOMATIC' ? 'automatic' : 'stepwise';
    qc.setQueryData([`flow-run-${kind}-record`, runId, record.id], record);
    void qc.invalidateQueries({ queryKey: [`flow-run-${kind}-records`, runId] });
    onClose();
    if (mode === 'AUTOMATIC') useWorkbenchStore.getState().openAutomaticRecord(runId, record.id);
    else useWorkbenchStore.getState().openStepwiseRecord(runId, record.id);
  };
  const importConfig = async (config: RecordConfigDocument) => {
    if (mode === 'AUTOMATIC') {
      if (config.format !== 'flowweave.continuous-record-config') throw new Error('这不是连续运行配置文件。');
      const records = await api.importAutomaticRecordConfigs(runId, config);
      records.forEach(record => qc.setQueryData(['flow-run-automatic-record', runId, record.id], record));
      void qc.invalidateQueries({ queryKey: ['flow-run-automatic-records', runId] });
      if (records[0]) created(records[0]);
    } else {
      if (config.format !== 'flowweave.stepwise-record-config') throw new Error('这不是逐步运行配置文件。');
      const records = await api.importStepwiseRecordConfigs(runId, config);
      records.forEach(record => qc.setQueryData(['flow-run-stepwise-record', runId, record.id], record));
      void qc.invalidateQueries({ queryKey: ['flow-run-stepwise-records', runId] });
      if (records[0]) created(records[0]);
    }
  };
  if (step === 'IMPORT') return <RecordConfigImportDialog mode={mode === 'MANUAL' ? 'STEPWISE' : 'AUTOMATIC'} onClose={onClose} onImport={importConfig}/>;
  if (step === 'CREATE' && run.data) return mode === 'AUTOMATIC'
    ? <AutomaticRecordDialog run={run.data} onClose={onClose} onCreated={created} onImport={() => setStep('IMPORT')}/>
    : <StepwiseRecordDialog run={run.data} onClose={onClose} onCreated={created} onImport={() => setStep('IMPORT')}/>;
  return <div className="modal-backdrop"><section className="modal record-mode-dialog" role="dialog" aria-modal="true" aria-label="选择运行类型"><header><div><span className="eyebrow">NEW RUN RECORD</span><h2>新建运行记录</h2><p>选择运行方式后，填写名称、起始节点与 Git 身份，也可导入配置。</p></div><button type="button" className="ghost" aria-label="关闭新建运行记录" onClick={onClose}><X size={17}/></button></header><RecordModeSelect value={mode} onChange={setMode}/>{run.isError && <p className="error">读取流程运行失败：{run.error.message}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" disabled={!run.data} onClick={() => setStep('CREATE')}>{run.isPending ? '正在加载…' : '继续创建'}</button></footer></section></div>;
}
