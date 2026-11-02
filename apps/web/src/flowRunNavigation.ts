export type FlowRunLocation = {
  runId: string;
  mode?: 'MANUAL' | 'AUTOMATIC';
  recordId?: string;
};

export function flowRunPath(location: FlowRunLocation): string {
  const root = `/flow-runs/${encodeURIComponent(location.runId)}`;
  return location.recordId && location.mode
    ? `${root}/records/${location.mode === 'AUTOMATIC' ? 'automatic' : 'stepwise'}/${encodeURIComponent(location.recordId)}`
    : root;
}

export function parseFlowRunPath(path: string): FlowRunLocation | undefined {
  const match = path.match(/^\/flow-runs\/([^/]+)(?:\/records\/(automatic|stepwise)\/([^/]+))?$/);
  if (!match) return undefined;
  try {
    return {
      runId: decodeURIComponent(match[1]),
      ...(match[3] ? { mode: match[2] === 'automatic' ? 'AUTOMATIC' as const : 'MANUAL' as const, recordId: decodeURIComponent(match[3]) } : {}),
    };
  } catch { return undefined; }
}

export function flowRunLocationFromState(state: unknown): FlowRunLocation | undefined {
  const source = (state as { flowweaveFlowRun?: Record<string, unknown> } | null)?.flowweaveFlowRun;
  if (typeof source?.runId !== 'string') return undefined;
  const recordId = source.mode === 'AUTOMATIC' ? source.automaticRecordId : source.stepwiseRecordId;
  return {
    runId: source.runId,
    ...(typeof recordId === 'string' && recordId ? { mode: source.mode === 'AUTOMATIC' ? 'AUTOMATIC' as const : 'MANUAL' as const, recordId } : {}),
  };
}

export function flowRunTabId(location: FlowRunLocation): string {
  // Complete IDs identify tabs; the short ID is only a display label.
  return location.recordId && location.mode
    ? `${location.runId}:${location.mode}:${location.recordId}`
    : location.runId;
}

export function flowRunTabLabel(runNo: number, location: FlowRunLocation): string {
  return `Run #${runNo}${location.recordId ? `_${location.recordId.slice(0, 8)}` : ''}`;
}
