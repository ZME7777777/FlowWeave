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
    useWorkbenchStore.setState({
      view: 'workbench',
      selectedRunId: flowRunId,
      selectedNodeRunId: nodeRunId,
      selectedAttemptId: attemptId,
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
