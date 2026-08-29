import {
  agentWorkspaceFileUrl,
  agentWorkspaceTerminalUrl,
  api,
  subscribeToAgentWorkspaceStream,
} from './client';

type AgentSessionApi = Pick<typeof api,
  | 'defaultAgentWorkspace'
  | 'agentWorkspaceRuntime'
  | 'agentConversations'
  | 'agentWorkDirectories'
  | 'providers'
  | 'capabilities'
  | 'agentWorkspaceCapabilities'
  | 'agentWorkspaceMcpReadiness'
  | 'replaceAgentWorkspaceCapabilities'
  | 'addAgentConversationCapability'
  | 'agentWorkspaceDetails'
  | 'createAgentWorkDirectory'
  | 'agentWorkspaceFilePreview'
  | 'closeAgentWorkspaceTerminal'
  | 'bootstrapAgentConversation'
  | 'updateAgentConversation'
  | 'deleteAgentConversation'
  | 'agentConversationEvents'
  | 'agentConversationInputReadiness'
  | 'agentConversationContext'
  | 'agentPendingConfirmation'
  | 'sendAgentMessage'
  | 'migrateAgentStreamingConversation'
  | 'uploadAgentAttachment'
  | 'uploadAgentWorkspaceAttachment'
  | 'forkAgentConversation'
  | 'condenseAgentConversation'
  | 'interruptAgentConversation'
  | 'resumeAgentConversation'
  | 'decideAgentConfirmation'
  | 'rerunAgentMessage'
  | 'switchAgentConversationModel'
>;

/**
 * A host adapter for the complete Agent-session UI. The workbench owns all
 * state and rendering; a host may only supply its existing transport.
 */
export interface AgentSessionGateway {
  readonly id: string;
  readonly api: AgentSessionApi;
  readonly terminalUrl: typeof agentWorkspaceTerminalUrl;
  readonly fileUrl: typeof agentWorkspaceFileUrl;
  readonly subscribe: typeof subscribeToAgentWorkspaceStream;
}

export const agentWorkspaceSessionGateway: AgentSessionGateway = {
  id: 'agent-workspace',
  api: {
    defaultAgentWorkspace: api.defaultAgentWorkspace,
    agentWorkspaceRuntime: api.agentWorkspaceRuntime,
    agentConversations: api.agentConversations,
    agentWorkDirectories: api.agentWorkDirectories,
    providers: api.providers,
    capabilities: api.capabilities,
    agentWorkspaceCapabilities: api.agentWorkspaceCapabilities,
    agentWorkspaceMcpReadiness: api.agentWorkspaceMcpReadiness,
    replaceAgentWorkspaceCapabilities: api.replaceAgentWorkspaceCapabilities,
    addAgentConversationCapability: api.addAgentConversationCapability,
    agentWorkspaceDetails: api.agentWorkspaceDetails,
    createAgentWorkDirectory: api.createAgentWorkDirectory,
    agentWorkspaceFilePreview: api.agentWorkspaceFilePreview,
    closeAgentWorkspaceTerminal: api.closeAgentWorkspaceTerminal,
    bootstrapAgentConversation: api.bootstrapAgentConversation,
    updateAgentConversation: api.updateAgentConversation,
    deleteAgentConversation: api.deleteAgentConversation,
    agentConversationEvents: api.agentConversationEvents,
    agentConversationInputReadiness: api.agentConversationInputReadiness,
    agentConversationContext: api.agentConversationContext,
    agentPendingConfirmation: api.agentPendingConfirmation,
    sendAgentMessage: api.sendAgentMessage,
    migrateAgentStreamingConversation: api.migrateAgentStreamingConversation,
    uploadAgentAttachment: api.uploadAgentAttachment,
    uploadAgentWorkspaceAttachment: api.uploadAgentWorkspaceAttachment,
    forkAgentConversation: api.forkAgentConversation,
    condenseAgentConversation: api.condenseAgentConversation,
    interruptAgentConversation: api.interruptAgentConversation,
    resumeAgentConversation: api.resumeAgentConversation,
    decideAgentConfirmation: api.decideAgentConfirmation,
    rerunAgentMessage: api.rerunAgentMessage,
    switchAgentConversationModel: api.switchAgentConversationModel,
  },
  terminalUrl: agentWorkspaceTerminalUrl,
  fileUrl: agentWorkspaceFileUrl,
  subscribe: subscribeToAgentWorkspaceStream,
};
