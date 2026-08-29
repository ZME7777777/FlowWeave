import {
  agentWorkspaceFileUrl,
  agentWorkspaceTerminalUrl,
  api,
  subscribeToAgentWorkspaceStream,
} from './client';

type AgentSessionApi = {
  readonly defaultHost: typeof api.defaultAgentWorkspace;
  readonly runtime: typeof api.agentWorkspaceRuntime;
  readonly conversations: typeof api.agentConversations;
  readonly workDirectories: typeof api.agentWorkDirectories;
  readonly providers: typeof api.providers;
  readonly capabilities: typeof api.capabilities;
  readonly hostCapabilities: typeof api.agentWorkspaceCapabilities;
  readonly mcpReadiness: typeof api.agentWorkspaceMcpReadiness;
  readonly replaceHostCapabilities: typeof api.replaceAgentWorkspaceCapabilities;
  readonly addConversationCapability: typeof api.addAgentConversationCapability;
  readonly workspaceDetails: typeof api.agentWorkspaceDetails;
  readonly createWorkDirectory: typeof api.createAgentWorkDirectory;
  readonly filePreview: typeof api.agentWorkspaceFilePreview;
  readonly closeTerminal: typeof api.closeAgentWorkspaceTerminal;
  readonly bootstrapConversation: typeof api.bootstrapAgentConversation;
  readonly updateConversation: typeof api.updateAgentConversation;
  readonly deleteConversation: typeof api.deleteAgentConversation;
  readonly conversationEvents: typeof api.agentConversationEvents;
  readonly inputReadiness: typeof api.agentConversationInputReadiness;
  readonly conversationContext: typeof api.agentConversationContext;
  readonly pendingConfirmation: typeof api.agentPendingConfirmation;
  readonly sendMessage: typeof api.sendAgentMessage;
  readonly migrateStreamingConversation: typeof api.migrateAgentStreamingConversation;
  readonly uploadConversationAttachment: typeof api.uploadAgentAttachment;
  readonly uploadDraftAttachment: typeof api.uploadAgentWorkspaceAttachment;
  readonly forkConversation: typeof api.forkAgentConversation;
  readonly condenseConversation: typeof api.condenseAgentConversation;
  readonly interruptConversation: typeof api.interruptAgentConversation;
  readonly resumeConversation: typeof api.resumeAgentConversation;
  readonly decideConfirmation: typeof api.decideAgentConfirmation;
  readonly rerunMessage: typeof api.rerunAgentMessage;
  readonly switchConversationModel: typeof api.switchAgentConversationModel;
};

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
    defaultHost: api.defaultAgentWorkspace,
    runtime: api.agentWorkspaceRuntime,
    conversations: api.agentConversations,
    workDirectories: api.agentWorkDirectories,
    providers: api.providers,
    capabilities: api.capabilities,
    hostCapabilities: api.agentWorkspaceCapabilities,
    mcpReadiness: api.agentWorkspaceMcpReadiness,
    replaceHostCapabilities: api.replaceAgentWorkspaceCapabilities,
    addConversationCapability: api.addAgentConversationCapability,
    workspaceDetails: api.agentWorkspaceDetails,
    createWorkDirectory: api.createAgentWorkDirectory,
    filePreview: api.agentWorkspaceFilePreview,
    closeTerminal: api.closeAgentWorkspaceTerminal,
    bootstrapConversation: api.bootstrapAgentConversation,
    updateConversation: api.updateAgentConversation,
    deleteConversation: api.deleteAgentConversation,
    conversationEvents: api.agentConversationEvents,
    inputReadiness: api.agentConversationInputReadiness,
    conversationContext: api.agentConversationContext,
    pendingConfirmation: api.agentPendingConfirmation,
    sendMessage: api.sendAgentMessage,
    migrateStreamingConversation: api.migrateAgentStreamingConversation,
    uploadConversationAttachment: api.uploadAgentAttachment,
    uploadDraftAttachment: api.uploadAgentWorkspaceAttachment,
    forkConversation: api.forkAgentConversation,
    condenseConversation: api.condenseAgentConversation,
    interruptConversation: api.interruptAgentConversation,
    resumeConversation: api.resumeAgentConversation,
    decideConfirmation: api.decideAgentConfirmation,
    rerunMessage: api.rerunAgentMessage,
    switchConversationModel: api.switchAgentConversationModel,
  },
  terminalUrl: agentWorkspaceTerminalUrl,
  fileUrl: agentWorkspaceFileUrl,
  subscribe: subscribeToAgentWorkspaceStream,
};
