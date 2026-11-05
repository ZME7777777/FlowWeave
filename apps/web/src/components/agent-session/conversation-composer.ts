import type { AgentSessionCapability, CapabilityAsset, ModelProvider } from '../../types';
import type { ComposerSuggestion } from './AgentComposerInput';

export function defaultConversationModel(
  providers: ModelProvider[],
  sourceProviderId?: string | null,
  sourceModelName?: string | null,
  sourceReasoningEffort?: string | null,
) {
  const source = providers.find(
    (provider) => provider.id === sourceProviderId && provider.connection_state === 'CONNECTED',
  );
  const provider = source ?? providers.find((candidate) => candidate.connection_state === 'CONNECTED');
  const model =
    provider?.models.find((candidate) => candidate.enabled && candidate.model_name === sourceModelName) ??
    provider?.models.find((candidate) => candidate.enabled && candidate.is_default) ??
    provider?.models.find((candidate) => candidate.enabled);
  return provider && model
    ? {
        providerId: provider.id,
        modelName: model.model_name,
        reasoningEffort: sourceReasoningEffort ?? model.default_reasoning_effort ?? null,
      }
    : undefined;
}

export function conversationComposerSuggestions(
  capabilities: AgentSessionCapability[] | undefined,
  catalogItems: CapabilityAsset[],
): ComposerSuggestion[] {
  const catalog = new Map(catalogItems.map((item) => [item.id, item]));
  const seen = new Set<string>();
  return (capabilities ?? []).flatMap<ComposerSuggestion>((reference) => {
    const capability = catalog.get(reference.id);
    const description = capability?.description || reference.capability_key;
    if (reference.capability_type === 'SKILL') {
      const id = `skill:${reference.id}`;
      if (seen.has(id)) return [];
      seen.add(id);
      return [
        {
          id,
          kind: 'SKILL' as const,
          token: `$${reference.capability_key}`,
          label: reference.capability_key,
          detail: description,
        },
      ];
    }
    if (reference.capability_type === 'MCP') {
      const id = `mcp:${reference.id}`;
      if (seen.has(id)) return [];
      seen.add(id);
      return [
        {
          id,
          kind: 'MCP' as const,
          token: `使用 MCP「${reference.capability_key}」：`,
          label: reference.capability_key,
          detail: `${description} · 以自然语言说明要执行的操作`,
        },
      ];
    }
    const contributions = capability?.document.contributions;
    const commands =
      contributions &&
      typeof contributions === 'object' &&
      Array.isArray((contributions as Record<string, unknown>).commands)
        ? ((contributions as Record<string, unknown>).commands as unknown[]).filter(
            (item): item is string => typeof item === 'string' && Boolean(item.trim()),
          )
        : [];
    const skills =
      contributions &&
      typeof contributions === 'object' &&
      Array.isArray((contributions as Record<string, unknown>).skills)
        ? ((contributions as Record<string, unknown>).skills as unknown[]).filter(
            (item): item is string => typeof item === 'string' && Boolean(item.trim()),
          )
        : [];
    return [
      ...commands.map((command) => ({
        id: `command:${reference.id}:${command}`,
        kind: 'COMMAND' as const,
        token: `/${reference.capability_key}:${command}`,
        label: command,
        detail: `${reference.capability_key} 命令 · ${description}`,
      })),
      ...skills.map((skill) => ({
        id: `plugin-skill:${reference.id}:${skill}`,
        kind: 'SKILL' as const,
        token: `$${skill}`,
        label: skill,
        detail: `${reference.capability_key} 提供的技能 · ${description}`,
      })),
    ].filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
  });
}
