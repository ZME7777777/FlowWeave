import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Circle, ClipboardList, ExternalLink, MessageSquareCode, PauseCircle, Plus, X } from 'lucide-react';
import { api } from '../api/client';
import type { Case, CaseStage } from '../types';
import { useEscapeClose } from '../components/useEscapeClose';

const CASE_STATE_LABELS: Record<Case['state'], string> = {
  DRAFT: '草稿', ACTIVE: '进行中', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVED: '已归档',
};
const STAGE_STATE_LABELS: Record<CaseStage['state'], string> = {
  PENDING: '待处理', ACTIVE: '进行中', COMPLETED: '已完成', SKIPPED: '已跳过',
};

function CaseCreateDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (item: Case) => void }) {
  useEscapeClose(onClose);
  const [name, setName] = useState('');
  const [objective, setObjective] = useState('');
  const [stageName, setStageName] = useState('梳理');
  const mutation = useMutation({
    mutationFn: () => api.createCase({
      name,
      objective,
      stages: stageName.trim() ? [{ stage_key: 'discover', name: stageName.trim(), objective: '' }] : [],
    }),
    onSuccess: onCreated,
  });
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="新建事项" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="modal case-create-dialog"><header><div><span className="eyebrow">NEW CASE</span><h2>新建事项</h2><p>事项属于当前用户；后续流程观察、阶段与协调会话都会归集在这里。</p></div><button className="ghost" aria-label="关闭新建事项" onClick={onClose}><X size={17}/></button></header><label>事项名称<input autoFocus value={name} maxLength={220} onChange={event => setName(event.target.value)} placeholder="例如：客户迁移准备"/></label><label>目标<textarea value={objective} maxLength={20_000} onChange={event => setObjective(event.target.value)} placeholder="说明需要达成的业务结果。"/></label><label>首个阶段<input value={stageName} maxLength={220} onChange={event => setStageName(event.target.value)} placeholder="例如：梳理"/></label>{mutation.error && <p className="error">{mutation.error.message}</p>}<footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={!name.trim() || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '创建中…' : '创建事项'}</button></footer></section></div>;
}

function CaseConversationSetup({ item, onUpdated, onOpen }: { item: Case; onUpdated: (item: Case) => void; onOpen: (bindingId: string) => void }) {
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const [providerId, setProviderId] = useState('');
  const provider = providers.data?.find(item => item.id === providerId) ?? providers.data?.find(item => item.available_for_nodes);
  const [modelName, setModelName] = useState('');
  const mutation = useMutation({
    mutationFn: () => api.createCaseControlConversation(item.id, {
      model_provider_id: providerId || provider?.id || '',
      model_name: modelName || provider?.models.find(model => model.enabled && model.is_default)?.model_name || provider?.models.find(model => model.enabled)?.model_name || '',
    }),
    onSuccess: result => {
      onUpdated(result);
      if (result.control_conversation_binding_id) onOpen(result.control_conversation_binding_id);
    },
  });
  if (item.control_conversation_binding_id) return <button className="primary" onClick={() => onOpen(item.control_conversation_binding_id!)}><MessageSquareCode size={14}/>进入协调会话</button>;
  const enabledModels = provider?.models.filter(model => model.enabled) ?? [];
  return <section className="case-conversation-setup"><h3>协调会话</h3><p>会话复用当前用户的 Agent Runtime，并冻结在本事项专属工作目录。</p><div className="case-conversation-fields"><label>模型服务<select value={providerId || provider?.id || ''} onChange={event => { setProviderId(event.target.value); setModelName(''); }}><option value="">选择模型服务</option>{providers.data?.filter(candidate => candidate.available_for_nodes).map(candidate => <option value={candidate.id} key={candidate.id}>{candidate.name}</option>)}</select></label><label>模型<select value={modelName || provider?.models.find(model => model.enabled && model.is_default)?.model_name || ''} onChange={event => setModelName(event.target.value)}><option value="">选择模型</option>{enabledModels.map(model => <option value={model.model_name} key={model.model_name}>{model.model_name}</option>)}</select></label></div>{mutation.error && <p className="error">{mutation.error.message}</p>}<button className="secondary" disabled={!provider || !(modelName || enabledModels.length) || mutation.isPending} onClick={() => mutation.mutate()}><MessageSquareCode size={14}/>{mutation.isPending ? '创建中…' : '创建协调会话'}</button></section>;
}

function CaseDetail({ item, onUpdated, onOpenConversation, onOpenFlowRun }: { item: Case; onUpdated: (item: Case) => void; onOpenConversation: (bindingId: string) => void; onOpenFlowRun: (flowRunId: string) => void }) {
  const runs = useQuery({ queryKey: ['flow-runs'], queryFn: api.runs });
  const [stageModes, setStageModes] = useState<Record<string, CaseStage['execution_mode']>>({});
  const setStage = useMutation({ mutationFn: ({ stage, state }: { stage: CaseStage; state: CaseStage['state'] }) => api.setCaseStageState(item.id, stage.id, item.row_version, state), onSuccess: onUpdated });
  const bindStageRun = useMutation({ mutationFn: ({ stage, flowRunId, mode }: { stage: CaseStage; flowRunId: string; mode: CaseStage['execution_mode'] }) => api.bindCaseStageFlowRun(item.id, stage.id, flowRunId, mode), onSuccess: onUpdated });
  return <section className="case-detail"><header><div><span className="eyebrow">CASE</span><h1>{item.name}</h1><p>{item.objective || '尚未填写事项目标。'}</p></div><span className={`case-state ${item.state.toLowerCase()}`}>{CASE_STATE_LABELS[item.state]}</span></header><section className="case-stages"><div className="case-section-heading"><h2>阶段路径</h2><small>每个阶段绑定一个顶层 FlowRun；其中可运行多个 NodeRun。</small></div>{item.stages.length ? <ol>{item.stages.map(stage => { const mode = stageModes[stage.id] ?? stage.execution_mode; const matchingRuns = (runs.data ?? []).filter(run => !item.stages.some(other => other.id !== stage.id && other.flow_run_id === run.id) && run.run_mode === (mode === 'STEPWISE' ? 'MANUAL' : 'AUTOMATIC')); return <li className={stage.state.toLowerCase()} key={stage.id}><span className="case-stage-marker">{stage.state === 'COMPLETED' ? <CheckCircle2 size={16}/> : stage.state === 'ACTIVE' ? <PauseCircle size={16}/> : <Circle size={16}/>}</span><div><b>{stage.name}</b><small>{stage.objective || STAGE_STATE_LABELS[stage.state]}</small><span className="case-stage-run"><select aria-label={`设置阶段 ${stage.name} 运行方式`} value={mode} disabled={Boolean(stage.flow_run_id) || bindStageRun.isPending} onChange={event => setStageModes(current => ({ ...current, [stage.id]: event.target.value as CaseStage['execution_mode'] }))}><option value="STEPWISE">逐步运行</option><option value="CONTINUOUS">连续运行</option></select>{stage.flow_run_id ? <button className="ghost case-stage-run-link" onClick={() => onOpenFlowRun(stage.flow_run_id!)}><ExternalLink size={12}/>打开 Run</button> : <select aria-label={`绑定阶段 ${stage.name} 的 FlowRun`} value="" disabled={bindStageRun.isPending || runs.isLoading} onChange={event => { const flowRunId = event.target.value; if (flowRunId) bindStageRun.mutate({ stage, flowRunId, mode }); }}><option value="">绑定 {mode === 'STEPWISE' ? '逐步' : '连续'} FlowRun</option>{matchingRuns.map(run => <option key={run.id} value={run.id}>{run.name} · {run.state}</option>)}</select>}</span></div><select aria-label={`设置阶段 ${stage.name} 状态`} value={stage.state} disabled={setStage.isPending} onChange={event => setStage.mutate({ stage, state: event.target.value as CaseStage['state'] })}>{Object.entries(STAGE_STATE_LABELS).map(([state, label]) => <option value={state} key={state}>{label}</option>)}</select></li>; })}</ol> : <div className="empty compact">尚未定义阶段。后续可由协调 Agent 提交受控的阶段建议。</div>}{setStage.error && <p className="error">{setStage.error.message}</p>}{bindStageRun.error && <p className="error">{bindStageRun.error.message}</p>}</section><CaseConversationSetup item={item} onUpdated={onUpdated} onOpen={onOpenConversation}/><section className="case-tasks"><div className="case-section-heading"><h2>协调任务</h2><small>运行事件与观察产物会归并为协调任务。</small></div>{item.tasks.length ? <ul>{item.tasks.map(task => <li key={task.id}><b>{task.task_key}</b><small>{task.task_type} · {task.state}</small></li>)}</ul> : <div className="empty compact">当前没有待协调任务。</div>}</section><section className="case-tasks"><div className="case-section-heading"><h2>自主决策审计</h2><small>每次受控纠偏均记录原因、结果和失败状态。</small></div>{item.decision_proposals.length ? <ul>{item.decision_proposals.map(decision => <li key={decision.id}><b>{decision.action} · {decision.reason}</b><small>{decision.state}{decision.executed_at ? ` · ${new Date(decision.executed_at).toLocaleString()}` : ''}</small></li>)}</ul> : <div className="empty compact">当前没有已执行的自主决策。</div>}</section></section>;
}

export function CasesPage({ onNavigate }: { onNavigate: (path: string) => void }) {
  const queryClient = useQueryClient();
  const cases = useQuery({ queryKey: ['cases'], queryFn: api.cases });
  const [selectedId, setSelectedId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const selected = useMemo(() => cases.data?.find(item => item.id === selectedId) ?? cases.data?.[0], [cases.data, selectedId]);
  const replace = (item: Case) => {
    queryClient.setQueryData<Case[]>(['cases'], current => current?.map(candidate => candidate.id === item.id ? item : candidate) ?? [item]);
    setSelectedId(item.id);
  };
  const openConversation = (caseId: string, bindingId: string) => {
    onNavigate(`/cases/${encodeURIComponent(caseId)}/conversations/${encodeURIComponent(bindingId)}`);
  };
  return <main className="cases-page"><header className="page-heading"><div><span className="eyebrow">CASE CONTROL PLANE</span><h1>事项</h1><p>围绕一个业务目标组织阶段、流程观察与协调会话。</p></div><button className="primary" onClick={() => setCreating(true)}><Plus size={15}/>新建事项</button></header><div className="cases-layout"><aside className="cases-list"><header><b>我的事项</b><small>{cases.data?.length ?? 0} 项</small></header>{cases.isLoading ? <div className="empty compact">正在加载事项…</div> : cases.error ? <p className="error">{cases.error.message}</p> : cases.data?.length ? <div>{cases.data.map(item => <button className={selected?.id === item.id ? 'active' : ''} key={item.id} onClick={() => setSelectedId(item.id)}><ClipboardList size={15}/><span><b>{item.name}</b><small>{CASE_STATE_LABELS[item.state]} · {item.stages.filter(stage => stage.state === 'COMPLETED').length}/{item.stages.length} 阶段</small></span></button>)}</div> : <div className="empty compact">还没有事项。创建一个事项来承接跨流程的工作目标。</div>}</aside>{selected ? <CaseDetail item={selected} onUpdated={replace} onOpenConversation={bindingId => openConversation(selected.id, bindingId)} onOpenFlowRun={flowRunId => onNavigate(`/flow-runs/${encodeURIComponent(flowRunId)}`)}/> : <section className="case-detail empty">选择或创建一个事项。</section>}</div>{creating && <CaseCreateDialog onClose={() => setCreating(false)} onCreated={item => { queryClient.setQueryData<Case[]>(['cases'], current => [item, ...(current ?? [])]); setSelectedId(item.id); setCreating(false); }}/>}</main>;
}
