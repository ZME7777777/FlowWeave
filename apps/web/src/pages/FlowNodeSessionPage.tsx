import { useCallback, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { flowNodeSessionGateway } from '../api/agent-session-gateway';
import { AgentSessionWorkbench } from '../components/agent-session/AgentSessionWorkbench';
import { flowNodeSessionHost } from '../components/agent-session/session-host';
import { useWorkbenchStore } from '../store/workbench';

/**
 * FlowRun node route host for the shared Agent workbench.  The route carries
 * only product lineage; the gateway resolves all Runtime and workspace facts
 * server-side through the node-Attempt scope.
 */
export function FlowNodeSessionPage({
  flowRunId,
  nodeRunId,
  attemptId,
  onNavigate,
}: {
  flowRunId: string;
  nodeRunId: string;
  attemptId: string;
  onNavigate: (path: string, replace?: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const host = useMemo(
    () => flowNodeSessionHost(flowRunId, nodeRunId, attemptId),
    [attemptId, flowRunId, nodeRunId],
  );
  const gateway = useMemo(
    () => flowNodeSessionGateway(flowRunId, attemptId),
    [attemptId, flowRunId],
  );
  const refreshFlowRunProjection = useCallback(async () => {
    // Node sessions are a separate route and query namespace.  Their native
    // pause/resume commands also mutate the parent Attempt/FlowRun projection,
    // including an automatic record hosted below another FlowRun.
    const sourceRunId = window.history.state?.flowweaveFlowRun?.runId;
    const refreshes: Promise<unknown>[] = [
      queryClient.invalidateQueries({ queryKey: ['flow-run', flowRunId] }),
      queryClient.invalidateQueries({ queryKey: ['runs'] }),
    ];
    if (typeof sourceRunId === 'string') {
      refreshes.push(
        queryClient.invalidateQueries({ queryKey: ['flow-run', sourceRunId] }),
        // The automatic-record query is inactive while this separate route is
        // open. Invalidation alone therefore leaves its old END_BLOCKED
        // value in the cache until after the Workbench first renders.
        queryClient.fetchQuery({
          queryKey: ['flow-run-automatic-records', sourceRunId],
          queryFn: () => api.automaticRecords(sourceRunId),
        }),
      );
    }
    await Promise.all(refreshes);
  }, [flowRunId, queryClient]);
  const returnToNodeAttempt = async () => {
    const source = window.history.state?.flowweaveFlowRun;
    const automatic = source?.mode === 'AUTOMATIC' && typeof source.automaticRecordId === 'string';
    useWorkbenchStore.setState({
      view: 'workbench',
      selectedRunId: typeof source?.runId === 'string' ? source.runId : flowRunId,
      selectedNodeRunId: nodeRunId,
      selectedAttemptId: attemptId,
      selectedWorkbenchMode: automatic ? 'AUTOMATIC' : 'MANUAL',
      selectedAutomaticRecordId: automatic ? source.automaticRecordId : undefined,
    });
    await refreshFlowRunProjection();
    onNavigate('/', true);
  };
  return <AgentSessionWorkbench
    gateway={gateway}
    host={host}
    onNavigate={onNavigate}
    onReturnToSource={returnToNodeAttempt}
    onHostStateChanged={refreshFlowRunProjection}
    autoOpenDraft
  />;
}
