import { useMemo } from 'react';
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
  const host = useMemo(
    () => flowNodeSessionHost(flowRunId, nodeRunId, attemptId),
    [attemptId, flowRunId, nodeRunId],
  );
  const gateway = useMemo(
    () => flowNodeSessionGateway(flowRunId, attemptId),
    [attemptId, flowRunId],
  );
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
    onNavigate('/', true);
  };
  return <AgentSessionWorkbench
    gateway={gateway}
    host={host}
    onNavigate={onNavigate}
    onReturnToSource={returnToNodeAttempt}
    autoOpenDraft
  />;
}
