import { createContext, useContext } from 'react';
import { agentWorkspaceSessionGateway, type AgentSessionGateway } from '../../api/agent-session-gateway';
import { agentWorkspaceSessionHost, type AgentSessionHost } from './session-host';

export const AgentSessionGatewayContext = createContext<AgentSessionGateway>(agentWorkspaceSessionGateway);
export const AgentSessionHostContext = createContext<AgentSessionHost>(agentWorkspaceSessionHost);

export function useAgentSessionGateway(): AgentSessionGateway {
  return useContext(AgentSessionGatewayContext);
}

export function useAgentSessionHost(): AgentSessionHost {
  return useContext(AgentSessionHostContext);
}
