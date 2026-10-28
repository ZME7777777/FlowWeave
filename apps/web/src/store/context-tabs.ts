import type { AuthUser } from '../types';
import { flowRunLocationFromState, flowRunPath, flowRunTabId, flowRunTabLabel, parseFlowRunPath } from '../flowRunNavigation';

export type SettingsView = 'environments' | 'credentials' | 'models' | 'ldap-users';
export type FlowRunTab = { id: string; label: string; path: string; state: unknown };
export type ContextTab = { kind: 'settings'; view: SettingsView } | { kind: 'flow-run'; id: string };
type ContextTabsSnapshot = { settings: SettingsView[]; runs: FlowRunTab[]; order: ContextTab[] };

const storageKey = (userId: string) => `flowweave.context-tabs.v1:${userId}`;
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  ? value as Record<string, unknown>
  : {};

// Only navigation metadata belongs in this snapshot, never arbitrary history state
// or page data. The route still performs its normal server authorization on open.
function navigationState(value: unknown) {
  const source = record(record(value).flowweaveFlowRun);
  if (typeof source.runId !== 'string') return {};
  return {
    flowweaveFlowRun: {
      runId: source.runId,
      nodeRunId: typeof source.nodeRunId === 'string' ? source.nodeRunId : undefined,
      attemptId: typeof source.attemptId === 'string' ? source.attemptId : undefined,
      mode: source.mode === 'AUTOMATIC' ? 'AUTOMATIC' : 'MANUAL',
      automaticRecordId: typeof source.automaticRecordId === 'string' ? source.automaticRecordId : undefined,
      stepwiseRecordId: typeof source.stepwiseRecordId === 'string' ? source.stepwiseRecordId : undefined,
    },
  };
}

export function readContextTabs(user: AuthUser): ContextTabsSnapshot {
  const snapshot: ContextTabsSnapshot = { settings: [], runs: [], order: [] };
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(storageKey(user.id)) ?? 'null');
    if (!Array.isArray(saved)) return snapshot;
    const seen = new Set<string>();
    for (const value of saved) {
      const tab = record(value);
      if (tab.kind === 'settings') {
        const view = tab.view;
        if (view !== 'environments' && view !== 'credentials' && view !== 'models' && !(view === 'ldap-users' && user.is_super_admin)) continue;
        if (seen.has(`settings:${view}`)) continue;
        seen.add(`settings:${view}`);
        snapshot.settings.push(view);
        snapshot.order.push({ kind: 'settings', view });
      } else if (tab.kind === 'flow-run' && typeof tab.id === 'string' && tab.id) {
        const state = navigationState(tab.state);
        const path = typeof tab.path === 'string' ? tab.path : `/flow-runs/${encodeURIComponent(tab.id)}`;
        const route = parseFlowRunPath(path);
        const savedLocation = flowRunLocationFromState(state);
        const location = route?.recordId ? route : savedLocation ?? route;
        if (!location) continue;
        const id = flowRunTabId(location);
        if (seen.has(`flow-run:${id}`)) continue;
        seen.add(`flow-run:${id}`);
        const root = flowRunPath(location);
        const leaf = path.match(/^\/flow-runs\/([^/]+)\/nodes\/([^/]+)\/attempts\/([^/]+)\/agent-sessions(?:\/[^/?#]+)?$/);
        const source = 'flowweaveFlowRun' in state ? state.flowweaveFlowRun : undefined;
        const validLeaf = leaf && (source?.nodeRunId && source.attemptId
          ? source.runId === location.runId && encodeURIComponent(source.nodeRunId) === leaf[2] && encodeURIComponent(source.attemptId) === leaf[3]
          : leaf[1] === encodeURIComponent(location.runId));
        const savedLabel = typeof tab.label === 'string' && tab.label ? tab.label : '正在打开运行…';
        const runNo = savedLabel.match(/^Run #(\d+)(?:_.*)?$/)?.[1];
        snapshot.runs.push({
          id, label: runNo ? flowRunTabLabel(Number(runNo), location) : savedLabel,
          path: validLeaf ? path : root,
          state: route || validLeaf ? state : {},
        });
        snapshot.order.push({ kind: 'flow-run', id });
      }
    }
  } catch { /* Missing, corrupt or unavailable storage starts with no tabs. */ }
  return snapshot;
}

export function saveContextTabs(userId: string, order: ContextTab[], runs: FlowRunTab[]) {
  try {
    sessionStorage.setItem(storageKey(userId), JSON.stringify(order.flatMap<ContextTab | ({ kind: 'flow-run' } & FlowRunTab)>(tab => {
      if (tab.kind === 'settings') return [tab];
      const run = runs.find(candidate => candidate.id === tab.id);
      return run ? [{ kind: 'flow-run', ...run, state: navigationState(run.state) }] : [];
    })));
  } catch { /* Keep the current tabs usable when browser storage is unavailable. */ }
}

export function clearContextTabs(userId: string) {
  try { sessionStorage.removeItem(storageKey(userId)); } catch { /* storage is unavailable */ }
}
