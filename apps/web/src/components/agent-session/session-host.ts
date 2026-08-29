/**
 * The browser-only identity of a session host. It deliberately owns only
 * navigation and recovery namespaces; API transport belongs to the gateway
 * and all conversation state remains in AgentSessionWorkbench.
 */
export interface AgentSessionHost {
  readonly id: string;
  readonly rootPath: string;
  readonly bootstrapRecoveryStorageKey: string;
  conversationPath(bindingId: string): string;
  bindingIdFromPathname(pathname: string): string | undefined;
}

export const agentWorkspaceSessionHost: AgentSessionHost = {
  id: 'agent-workspace',
  rootPath: '/agent',
  bootstrapRecoveryStorageKey: 'flowweave.agent.bootstrap-recovery.v1',
  conversationPath: bindingId => `/agent/conversations/${encodeURIComponent(bindingId)}`,
  bindingIdFromPathname: pathname => {
    const match = pathname.match(/^\/agent\/conversations\/([^/]+)$/);
    return match ? decodeURIComponent(match[1]) : undefined;
  },
};
