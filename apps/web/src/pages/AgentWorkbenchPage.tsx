import { AgentSessionWorkbench, type AgentSessionWorkbenchProps } from '../components/agent-session/AgentSessionWorkbench';

/**
 * The `/agent` route host. Keep this intentionally thin: it preserves the
 * existing route while the shared session workbench owns every interaction.
 */
export function AgentWorkbenchPage(props: AgentSessionWorkbenchProps) {
  return <AgentSessionWorkbench {...props}/>;
}
