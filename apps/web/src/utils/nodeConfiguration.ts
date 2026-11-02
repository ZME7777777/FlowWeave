import { ApiError } from '../api/client';
import type { AgentPreset, GatePolicy } from '../types';

export type ConfigurationField = 'inputs' | 'prompt' | 'model' | 'fallback' | 'capabilities' | 'context' | 'gates';
export type ConfigurationIssue = { field: ConfigurationField; message: string };
export const configurationTab = (field: ConfigurationField) => field === 'gates' ? 'gates' as const : ['model', 'fallback', 'capabilities'].includes(field) ? 'agent' as const : 'inputs' as const;

export function configurationIssues(prompt: string, preset: AgentPreset, gates: GatePolicy[]): ConfigurationIssue[] {
  const issues: ConfigurationIssue[] = [];
  if (!prompt.trim() || prompt.length > 200_000) issues.push({ field: 'prompt', message: prompt.trim() ? '启动提示词不能超过 200000 字。' : '请填写启动提示词。' });
  if (!preset.model_provider_id || !preset.model_name) issues.push({ field: 'model', message: '请选择模型供应商和模型。' });
  if (preset.node_context_prompt && preset.node_context_prompt.length > 200_000) issues.push({ field: 'context', message: '专属上下文不能超过 200000 字。' });
  const models = [preset, ...(preset.fallback_models ?? [])];
  const identities = models.map(item => JSON.stringify([item.model_provider_id, item.model_name, item.reasoning_effort || '']));
  if ((preset.fallback_models?.length ?? 0) > 3 || models.slice(1).some(item => !item.model_provider_id || !item.model_name) || new Set(identities).size !== identities.length) issues.push({ field: 'fallback', message: '请补齐备用模型，并移除重复的模型配置（最多 3 项）。' });
  gates.forEach((gate, index) => {
    if (!String(gate.config.prompt ?? '').trim() || !gate.agent_preset?.model_provider_id || !gate.agent_preset.model_name) issues.push({ field: 'gates', message: `第 ${index + 1} 项门禁需要判定提示词、模型供应商和模型。` });
  });
  return issues;
}
export function configurationErrorIssues(error: unknown): ConfigurationIssue[] {
  if (!(error instanceof ApiError)) return [];
  const errors = Array.isArray(error.details.errors) ? error.details.errors : [];
  const fields = errors.map(item => JSON.stringify((item as { loc?: unknown }).loc ?? ''));
  const domain = error.code;
  if (domain !== 'INVALID_COMMAND' && !/INPUT_|MODEL_|PROVIDER_|CAPABILITY_|AGENT_PRESET/.test(domain)) return [];
  if (!fields.length) fields.push(domain);
  return fields.map(path => {
    const field: ConfigurationField = /gates/i.test(path) ? 'gates' : /fallback/i.test(path) ? 'fallback' : /capabilit/i.test(path) ? 'capabilities' : /context/i.test(path) ? 'context' : /prompt/i.test(path) ? 'prompt' : /artifact|input/i.test(path) ? 'inputs' : 'model';
    return { field, message: ({ inputs: '请检查节点输入的格式与绑定。', prompt: '请检查启动提示词是否完整且未超长。', model: '请选择当前可用的模型供应商和模型。', fallback: '请检查备用模型是否完整、可用且没有重复。', capabilities: '部分能力版本不可用，请重新选择。', context: '请检查专属上下文是否超长。', gates: '请检查门禁的提示词、模型和执行配置。' })[field] };
  });
}
