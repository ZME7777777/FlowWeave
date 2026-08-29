import {
  type AgentStreamEvent,
  agentWorkspaceFileUrl,
  agentWorkspaceTerminalUrl,
  api,
  subscribeToAgentWorkspaceStream,
} from './client';
import type {
  AgentAttachment,
  AgentConversation,
  AgentConversationContext,
  AgentConversationInputReadiness,
  AgentPendingConfirmation,
  AgentSessionCapability,
  AgentSessionHostDetails,
  AgentSessionMcpReadiness,
  AgentSessionRuntime,
  AgentSessionWorkDirectory,
  AgentSessionWorkDirectoryList,
  AgentSessionWorkspaceDetails,
  CapabilityAsset,
  ModelProvider,
  OpenHandsConversationEventBatch,
} from '../types';

export type AgentSessionHostId = string;
export type AgentSessionBindingId = string;
export type AgentSessionWorkDirectoryId = string;
export type AgentSessionFileOptions = {
  bindingId?: AgentSessionBindingId;
  workDirectoryId?: AgentSessionWorkDirectoryId;
  download?: boolean;
};
export type AgentSessionTerminalOptions = Omit<AgentSessionFileOptions, 'download'> & {
  terminalInstanceId: string;
};
export type AgentSessionStreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';

/**
 * The complete, host-neutral browser transport contract for the shared
 * session workbench. Host adapters may use different routes, but must return
 * these DTOs and preserve the same OpenHands session semantics.
 */
export interface AgentSessionApi {
  readonly defaultHost: () => Promise<AgentSessionHostDetails>;
  readonly runtime: (hostId: AgentSessionHostId) => Promise<AgentSessionRuntime>;
  readonly conversations: (hostId: AgentSessionHostId) => Promise<AgentConversation[]>;
  readonly workDirectories: (hostId: AgentSessionHostId) => Promise<AgentSessionWorkDirectoryList>;
  readonly providers: () => Promise<ModelProvider[]>;
  readonly capabilities: () => Promise<CapabilityAsset[]>;
  readonly hostCapabilities: (hostId: AgentSessionHostId) => Promise<AgentSessionCapability[]>;
  readonly mcpReadiness: (hostId: AgentSessionHostId, capabilityVersionId: string) => Promise<AgentSessionMcpReadiness>;
  readonly replaceHostCapabilities: (hostId: AgentSessionHostId, capabilityVersionIds: string[]) => Promise<AgentSessionCapability[]>;
  readonly addConversationCapability: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, capabilityVersionId: string) => Promise<AgentConversation>;
  readonly workspaceDetails: (hostId: AgentSessionHostId, options?: Omit<AgentSessionFileOptions, 'download'>) => Promise<AgentSessionWorkspaceDetails>;
  readonly createWorkDirectory: (hostId: AgentSessionHostId, displayName: string, selectedPaths: string[]) => Promise<AgentSessionWorkDirectory>;
  readonly filePreview: (hostId: AgentSessionHostId, path: string, options?: Omit<AgentSessionFileOptions, 'download'>) => Promise<string>;
  readonly closeTerminal: (hostId: AgentSessionHostId, terminalInstanceId: string, options?: Omit<AgentSessionFileOptions, 'download'>) => Promise<void>;
  readonly bootstrapConversation: (hostId: AgentSessionHostId, conversationId: string, modelProviderId: string, modelName: string, reasoningEffort: string | null, content: string, attachments?: AgentAttachment[], workDirectoryId?: AgentSessionWorkDirectoryId, idempotencyKey?: string) => Promise<{ conversation: AgentConversation; accepted: boolean; cursor?: string | null }>;
  readonly updateConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, title: string) => Promise<AgentConversation>;
  readonly deleteConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<void>;
  readonly conversationEvents: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, cursor?: string) => Promise<OpenHandsConversationEventBatch>;
  readonly inputReadiness: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<AgentConversationInputReadiness>;
  readonly conversationContext: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<AgentConversationContext>;
  readonly pendingConfirmation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<AgentPendingConfirmation>;
  readonly sendMessage: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, content: string, attachments?: AgentAttachment[]) => Promise<{ accepted: boolean; cursor?: string | null; compacted?: boolean }>;
  readonly migrateStreamingConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, modelProviderId: string, modelName?: string | null, reasoningEffort?: string | null) => Promise<AgentConversation>;
  readonly uploadConversationAttachment: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, file: File) => Promise<AgentAttachment>;
  readonly uploadDraftAttachment: (hostId: AgentSessionHostId, file: File, workDirectoryId?: AgentSessionWorkDirectoryId, conversationId?: string) => Promise<AgentAttachment>;
  readonly forkConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, eventId: string) => Promise<AgentConversation>;
  readonly condenseConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<{ accepted: boolean; cursor?: string | null }>;
  readonly interruptConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<{ accepted: boolean }>;
  readonly resumeConversation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId) => Promise<{ accepted: boolean; cursor?: string | null }>;
  readonly decideConfirmation: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, expectedPendingDigest: string, accept: boolean, reason: string) => Promise<{ accepted: boolean; cursor?: string | null }>;
  readonly rerunMessage: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, eventId: string, content: string) => Promise<{ accepted: boolean; cursor?: string | null }>;
  readonly switchConversationModel: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, modelProviderId: string, modelName: string, reasoningEffort: string | null) => Promise<{ model_provider_id: string; model_name?: string | null; reasoning_effort?: string | null }>;
}

/**
 * A host adapter for the complete Agent-session UI. The workbench owns all
 * state and rendering; a host may only supply its existing transport.
 */
export interface AgentSessionGateway {
  readonly id: string;
  readonly api: AgentSessionApi;
  readonly terminalUrl: (hostId: AgentSessionHostId, rows: number | undefined, columns: number | undefined, options: AgentSessionTerminalOptions) => string;
  readonly fileUrl: (hostId: AgentSessionHostId, path: string, options?: AgentSessionFileOptions) => string;
  readonly subscribe: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, onEvent: (event: AgentStreamEvent) => void, onStatus?: (status: AgentSessionStreamStatus) => void) => () => void;
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
