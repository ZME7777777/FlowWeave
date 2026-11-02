import { Bot, FileText, Plug, Puzzle, Sparkles, Webhook } from 'lucide-react';

export const capabilityTypeIcons = { SKILL: Sparkles, MCP: Plug, PLUGIN: Puzzle, CONTEXT: FileText, AGENT_DEFINITION: Bot, HOOK: Webhook };
export const capabilityTypeLabels = { SKILL: 'Skill', MCP: 'MCP', PLUGIN: 'Plugin', CONTEXT: 'Context', AGENT_DEFINITION: 'Agent', HOOK: 'Hook' };
export type AgentCapabilityType = keyof typeof capabilityTypeLabels;
export const capabilityTypeFilters = [['ALL', 'all'], ['PLUGIN', 'plugin'], ['MCP', 'MCP'], ['SKILL', 'skill'], ['CONTEXT', 'context'], ['AGENT_DEFINITION', 'agent'], ['HOOK', 'hook']] as const;
