import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from '@xyflow/react';
import type { FlowMappingEdgeData } from './flowMappingEdgeLayout';

export function FlowMappingEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  style,
  data,
}: EdgeProps<Edge<FlowMappingEdgeData>>) {
  const [path] = getBezierPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
  });
  const fraction = data?.labelFraction ?? 0.5;
  const deltaX = targetX - sourceX;
  const deltaY = targetY - sourceY;
  const distance = Math.hypot(deltaX, deltaY) || 1;
  const laneOffset = data?.labelLane ?? 0;
  // Position the label on a normal to the source-target span. This keeps rows
  // separated even when nodes are laid out vertically or diagonally.
  const labelX = sourceX + deltaX * fraction - (deltaY / distance) * laneOffset;
  const labelY = sourceY + deltaY * fraction + (deltaX / distance) * laneOffset;
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style}/>
    <EdgeLabelRenderer>
      <div
        className="flow-mapping-edge-label nodrag nopan"
        style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
      >
        {data?.label}
      </div>
    </EdgeLabelRenderer>
  </>;
}
