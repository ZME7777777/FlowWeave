import {
  type AgentStreamEvent,
  agentWorkspaceFileUrl,
  agentWorkspaceTerminalUrl,
  api,
  nodeSessionApi,
  subscribeToNodeSessionStream,
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

/** Product capabilities deliberately supplied by each session host. */
export interface AgentSessionFeatures {
  readonly workDirectories: boolean;
  readonly capabilities: boolean;
  readonly attachments: boolean;
  readonly modelSelection: boolean;
  readonly conversationDeletion: boolean;
  readonly fork: boolean;
  readonly rewrite: boolean;
  readonly confirmations: boolean;
  readonly terminalRequiresConversation: boolean;
}

const fullSessionFeatures: AgentSessionFeatures = {
  workDirectories: true, capabilities: true, attachments: true, modelSelection: true,
  conversationDeletion: true, fork: true, rewrite: true, confirmations: true,
  terminalRequiresConversation: false,
};

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
  readonly features: AgentSessionFeatures;
  readonly api: AgentSessionApi;
  readonly terminalUrl: (hostId: AgentSessionHostId, rows: number | undefined, columns: number | undefined, options: AgentSessionTerminalOptions) => string;
  readonly fileUrl: (hostId: AgentSessionHostId, path: string, options?: AgentSessionFileOptions) => string;
  readonly subscribe: (hostId: AgentSessionHostId, bindingId: AgentSessionBindingId, onEvent: (event: AgentStreamEvent) => void, onStatus?: (status: AgentSessionStreamStatus) => void) => () => void;
}

export const agentWorkspaceSessionGateway: AgentSessionGateway = {
  id: 'agent-workspace',
  features: fullSessionFeatures,
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

const unavailable = (operation: string): never => {
  throw new Error(`${operation} is unavailable for a FlowRun node session`);
};

/**
 * Transport for one immutable FlowRun node-Attempt scope. Every browser
 * request carries the same server-authorized Run/Attempt lineage; binding IDs
 * never become globally addressable through this adapter.
 */
export function flowNodeSessionGateway(
  flowRunId: string,
  attemptId: string,
): AgentSessionGateway {
  return {
    id: `flow-node:${flowRunId}:${attemptId}`,
    features: {
      workDirectories: true, capabilities: false, attachments: false,
      modelSelection: false, conversationDeletion: false, fork: false,
      rewrite: false, confirmations: false, terminalRequiresConversation: true,
    },
    api: {
      defaultHost: () => nodeSessionApi.host(flowRunId, attemptId),
      runtime: () => nodeSessionApi.runtime(flowRunId, attemptId),
      conversations: () => nodeSessionApi.conversations(flowRunId, attemptId),
      workDirectories: () => nodeSessionApi.workDirectories(flowRunId, attemptId),
      providers: async () => [],
      capabilities: async () => [],
      hostCapabilities: async () => [],
      mcpReadiness: async () => unavailable('Capability readiness'),
      replaceHostCapabilities: async () => unavailable('Capability replacement'),
      addConversationCapability: async () => unavailable('Conversation capability registration'),
      workspaceDetails: (_hostId, options) =>
        nodeSessionApi.workspace(flowRunId, attemptId, options?.bindingId, options?.workDirectoryId),
      createWorkDirectory: async (_hostId, displayName, selectedPaths) =>
        nodeSessionApi.createWorkDirectory(flowRunId, attemptId, displayName, selectedPaths),
      filePreview: async (_hostId, path, options) => {
        const response = await fetch(nodeSessionApi.file(
          flowRunId, attemptId, path, options?.bindingId, options?.workDirectoryId, false,
        ));
        if (!response.ok) throw new Error('Node workspace file preview is unavailable');
        return response.text();
      },
      // Node terminals are browser-owned websocket instances. Closing a tab
      // closes its socket; there is no persistent Workspace terminal record.
      closeTerminal: async () => undefined,
      bootstrapConversation: async (_hostId, conversationId, _providerId, modelName, reasoningEffort, content, _attachments, workDirectoryId, idempotencyKey) => {
        const conversation = await nodeSessionApi.create(
          flowRunId, attemptId, undefined, modelName || undefined, reasoningEffort, idempotencyKey ?? conversationId, workDirectoryId,
        );
        const sent = content.trim()
          ? await nodeSessionApi.message(flowRunId, attemptId, conversation.id, content, idempotencyKey)
          : { accepted: true, cursor: null };
        return { conversation, accepted: sent.accepted, cursor: sent.cursor };
      },
      updateConversation: (_hostId, bindingId, title) =>
        nodeSessionApi.update(flowRunId, attemptId, bindingId, title),
      deleteConversation: async () => unavailable('Conversation deletion'),
      conversationEvents: (_hostId, bindingId, cursor) =>
        nodeSessionApi.events(flowRunId, attemptId, bindingId, cursor),
      inputReadiness: (_hostId, bindingId) =>
        nodeSessionApi.inputReadiness(flowRunId, attemptId, bindingId),
      conversationContext: (_hostId, bindingId) =>
        nodeSessionApi.context(flowRunId, attemptId, bindingId),
      pendingConfirmation: async () => ({ pending: false }),
      sendMessage: async (_hostId, bindingId, content, attachments = []) => {
        if (attachments.length) unavailable('Conversation attachments');
        return nodeSessionApi.message(flowRunId, attemptId, bindingId, content);
      },
      migrateStreamingConversation: async () => unavailable('Streaming migration'),
      uploadConversationAttachment: async () => unavailable('Conversation attachments'),
      uploadDraftAttachment: async () => unavailable('Draft attachments'),
      forkConversation: async () => unavailable('Conversation forks'),
      condenseConversation: (_hostId, bindingId) =>
        nodeSessionApi.condense(flowRunId, attemptId, bindingId),
      interruptConversation: (_hostId, bindingId) =>
        nodeSessionApi.interrupt(flowRunId, attemptId, bindingId),
      resumeConversation: (_hostId, bindingId) =>
        nodeSessionApi.resume(flowRunId, attemptId, bindingId),
      decideConfirmation: async () => unavailable('Tool confirmations'),
      rerunMessage: async () => unavailable('Message rewriting'),
      switchConversationModel: async () => unavailable('Model switching'),
    },
    terminalUrl: (_hostId, rows, columns, options) => {
      if (!options.bindingId) return unavailable('Node terminal without a conversation');
      return nodeSessionApi.terminal(flowRunId, attemptId, options.bindingId, rows, columns);
    },
    fileUrl: (_hostId, path, options) =>
      nodeSessionApi.file(flowRunId, attemptId, path, options?.bindingId, options?.workDirectoryId, options?.download),
    subscribe: (_hostId, bindingId, onEvent, onStatus) =>
      subscribeToNodeSessionStream(flowRunId, attemptId, bindingId, onEvent, onStatus),
  };
}
