/**
 * The browser-only identity of a session host. It deliberately owns only
 * navigation and recovery namespaces; API transport belongs to the gateway
 * and all conversation state remains in AgentSessionWorkbench.
 */
export interface AgentSessionHost {
  /** Stable host adapter identity, never a Runtime/container identity. */
  readonly id: string;
  readonly displayName: string;
  readonly rootPath: string;
  readonly bootstrapRecoveryStorageKey: string;
  /** Namespace every query and browser-only tool layout by host identity. */
  queryKey(resource: string, ...identifiers: Array<string | undefined>): readonly string[];
  workspaceToolsStorageKey(hostId: string): string;
  conversationPath(bindingId: string): string;
  bindingIdFromPathname(pathname: string): string | undefined;
}

export const agentWorkspaceSessionHost: AgentSessionHost = {
  id: 'agent-workspace',
  displayName: 'Agent 工作区',
  rootPath: '/agent',
  bootstrapRecoveryStorageKey: 'flowweave.agent.bootstrap-recovery.v1',
  queryKey: (resource, ...identifiers) => [
    'agent-session',
    'agent-workspace',
    resource,
    ...identifiers.filter((value): value is string => Boolean(value)),
  ],
  workspaceToolsStorageKey: hostId => `flowweave:agent-session-tools:agent-workspace:${hostId}`,
  conversationPath: bindingId => `/agent/conversations/${encodeURIComponent(bindingId)}`,
  bindingIdFromPathname: pathname => {
    const match = pathname.match(/^\/agent\/conversations\/([^/]+)$/);
    return match ? decodeURIComponent(match[1]) : undefined;
  },
};

export function flowNodeSessionHost(
  flowRunId: string,
  nodeRunId: string,
  attemptId: string,
): AgentSessionHost {
  const rootPath = `/flow-runs/${encodeURIComponent(flowRunId)}/nodes/${encodeURIComponent(nodeRunId)}/attempts/${encodeURIComponent(attemptId)}/agent-sessions`;
  const identity = `flow-node:${flowRunId}:${attemptId}`;
  return {
    id: identity,
    displayName: '节点会话',
    rootPath,
    bootstrapRecoveryStorageKey: `flowweave.node-session.bootstrap-recovery.v1:${flowRunId}:${attemptId}`,
    queryKey: (resource, ...identifiers) => [
      'agent-session', identity, resource,
      ...identifiers.filter((value): value is string => Boolean(value)),
    ],
    workspaceToolsStorageKey: hostId => `flowweave:agent-session-tools:${identity}:${hostId}`,
    conversationPath: bindingId => `${rootPath}/${encodeURIComponent(bindingId)}`,
    bindingIdFromPathname: pathname => {
      const expression = new RegExp(`^${rootPath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/([^/]+)$`);
      const match = pathname.match(expression);
      return match ? decodeURIComponent(match[1]) : undefined;
    },
  };
}
