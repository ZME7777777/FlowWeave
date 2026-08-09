import { create } from 'zustand';
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware';
import type { ViewName } from '../types';

const STORAGE_KEY = 'flowweave-workbench';
const STORAGE_VERSION = 2;
const VIEWS = new Set<ViewName>(['nodes', 'capabilities', 'environments', 'models', 'flows', 'runs', 'workbench', 'agent-chat']);

interface WorkbenchState {
  view: ViewName;
  selectedRunId?: string;
  selectedNodeRunId?: string;
  selectedAttemptId?: string;
  selectedConversationId?: string;
  setView: (view: ViewName) => void;
  openRun: (runId: string, nodeRunId?: string) => void;
  selectNodeRun: (id: string) => void;
  selectAttempt: (id: string) => void;
  selectExecution: (nodeRunId: string, attemptId?: string) => void;
  openAgentChat: (runId: string, nodeRunId: string, attemptId: string, conversationId?: string) => void;
  selectConversation: (id: string) => void;
  returnToWorkbench: () => void;
}

type PersistedWorkbenchState = Pick<
  WorkbenchState,
  'view' | 'selectedRunId' | 'selectedNodeRunId' | 'selectedAttemptId' | 'selectedConversationId'
>;

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function sanitizePersistedState(value: unknown): PersistedWorkbenchState {
  const state = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  return {
    view: typeof state.view === 'string' && VIEWS.has(state.view as ViewName)
      ? state.view as ViewName
      : 'nodes',
    selectedRunId: optionalString(state.selectedRunId),
    selectedNodeRunId: optionalString(state.selectedNodeRunId),
    selectedAttemptId: optionalString(state.selectedAttemptId),
    selectedConversationId: optionalString(state.selectedConversationId),
  };
}

const safeLocalStorage: StateStorage = {
  getItem: name => {
    try {
      const value = window.localStorage.getItem(name);
      if (value !== null) JSON.parse(value);
      return value;
    } catch {
      try { window.localStorage.removeItem(name); } catch { /* storage is unavailable */ }
      return null;
    }
  },
  setItem: (name, value) => {
    try { window.localStorage.setItem(name, value); } catch { /* keep in-memory state */ }
  },
  removeItem: name => {
    try { window.localStorage.removeItem(name); } catch { /* storage is unavailable */ }
  },
};

export const useWorkbenchStore = create<WorkbenchState>()(
  persist<WorkbenchState, [], [], PersistedWorkbenchState>(
    set => ({
      view: 'nodes',
      setView: view => set({ view }),
      openRun: (selectedRunId, selectedNodeRunId) => set({
        view: 'workbench', selectedRunId, selectedNodeRunId, selectedAttemptId: undefined,
      }),
      selectNodeRun: selectedNodeRunId => set({ selectedNodeRunId, selectedAttemptId: undefined }),
      selectAttempt: selectedAttemptId => set({ selectedAttemptId }),
      selectExecution: (selectedNodeRunId, selectedAttemptId) => set({
        selectedNodeRunId, selectedAttemptId,
      }),
      openAgentChat: (selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId) => set({
        view: 'agent-chat', selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId,
      }),
      selectConversation: selectedConversationId => set({ selectedConversationId }),
      returnToWorkbench: () => set({ view: 'workbench' }),
    }),
    {
      name: STORAGE_KEY,
      version: STORAGE_VERSION,
      storage: createJSONStorage<PersistedWorkbenchState>(() => safeLocalStorage),
      partialize: ({ view, selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId }) => ({
        view, selectedRunId, selectedNodeRunId, selectedAttemptId, selectedConversationId,
      }),
      migrate: persisted => sanitizePersistedState(persisted),
      merge: (persisted, current) => ({ ...current, ...sanitizePersistedState(persisted) }),
      onRehydrateStorage: () => (_state, error) => {
        if (error) safeLocalStorage.removeItem(STORAGE_KEY);
      },
    },
  ),
);
