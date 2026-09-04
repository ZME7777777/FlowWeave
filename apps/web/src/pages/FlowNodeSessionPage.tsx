import { useCallback, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';
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
  const refreshFlowRunProjection = useCallback(() => {
    // Node sessions are a separate route and query namespace.  Their native
    // pause/resume commands also mutate the parent Attempt/FlowRun projection,
    // including an automatic record hosted below another FlowRun.
    const sourceRunId = window.history.state?.flowweaveFlowRun?.runId;
    void queryClient.invalidateQueries({ queryKey: ['flow-run', flowRunId] });
    if (typeof sourceRunId === 'string') {
      void queryClient.invalidateQueries({ queryKey: ['flow-run', sourceRunId] });
      void queryClient.invalidateQueries({ queryKey: ['flow-run-automatic-records', sourceRunId] });
    }
    void queryClient.invalidateQueries({ queryKey: ['runs'] });
  }, [flowRunId, queryClient]);
  const returnToNodeAttempt = () => {
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
    refreshFlowRunProjection();
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
