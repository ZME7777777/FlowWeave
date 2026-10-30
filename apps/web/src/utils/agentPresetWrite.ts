import type { AgentPreset, GatePolicy } from '../types';

/** Read projections include frozen audit fields; commands accept only editable fields. */
export function writableAgentPreset(preset: AgentPreset): AgentPreset {
  return {
    capability_version_ids: [...(preset.capability_version_ids ?? [])],
    model_provider_id: preset.model_provider_id, model_name: preset.model_name,
    reasoning_effort: preset.reasoning_effort, node_context_enabled: preset.node_context_enabled ?? false,
    node_context_prompt: preset.node_context_prompt,
    fallback_models: preset.fallback_models?.map(item => ({ model_provider_id: item.model_provider_id, model_name: item.model_name, reasoning_effort: item.reasoning_effort })),
  };
}
export function writableGates(gates: GatePolicy[]): GatePolicy[] {
  return gates.map(gate => ({ stage: gate.stage, position: gate.position, gate_type: gate.gate_type,
    enabled: gate.enabled, timeout_seconds: gate.timeout_seconds, config: gate.config,
    agent_preset: gate.agent_preset ? { model_provider_id: gate.agent_preset.model_provider_id, model_name: gate.agent_preset.model_name, reasoning_effort: gate.agent_preset.reasoning_effort } : gate.agent_preset,
  }));
}
