/**
 * A started rerun keeps its draft (with `rerun_source_node_run_id`) for audit.
 * The node is only still "being rerun" while its newest non-cancelled execution
 * is the rerun source; once a newer execution exists, the node must show that
 * execution (and its session entry) instead of the stale draft.
 */
export function activeRerunSource(
  sourceNodeRunId: string | null | undefined,
  nodeExecutions: ReadonlyArray<{ id: string; state: string }>,
): string | undefined {
  if (!sourceNodeRunId) return undefined;
  const latest = nodeExecutions.filter(item => item.state !== 'CANCELLED').at(-1);
  return latest?.id === sourceNodeRunId ? sourceNodeRunId : undefined;
}
