import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from '@xyflow/react';

export type FlowMappingEdgeData = { label: string; labelOffsetX: number; labelOffsetY: number };

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
  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
  });
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style}/>
    <EdgeLabelRenderer>
      <div
        className="flow-mapping-edge-label nodrag nopan"
        style={{ transform: `translate(-50%, -50%) translate(${labelX + (data?.labelOffsetX ?? 0)}px, ${labelY + (data?.labelOffsetY ?? 0)}px)` }}
      >
        {data?.label}
      </div>
    </EdgeLabelRenderer>
  </>;
}

export const flowMappingEdgeTypes = { mappingEdge: FlowMappingEdge };

/** Separate labels for parallel mappings so their midpoint labels never overlap. */
export function withMappingLabelOffsets(edges: Edge[]): Array<Edge<FlowMappingEdgeData>> {
  const key = (edge: Edge) => `${edge.source}→${edge.target}`;
  const counts = new Map<string, number>();
  edges.forEach(edge => counts.set(key(edge), (counts.get(key(edge)) ?? 0) + 1));
  const positions = new Map<string, number>();
  return edges.map(edge => {
    const group = key(edge);
    const position = positions.get(group) ?? 0;
    positions.set(group, position + 1);
    const count = counts.get(group) ?? 1;
    const label = typeof edge.label === 'string' ? edge.label : `${edge.sourceHandle?.replace('output:', '') ?? ''} → ${edge.targetHandle?.replace('input:', '') ?? ''}`;
    return {
      ...edge,
      type: 'mappingEdge',
      label: undefined,
      data: {
        label,
        // Parallel Bezier paths cross near their midpoint. Separate labels on
        // both axes so their backgrounds cannot cover one another.
        labelOffsetX: (position - (count - 1) / 2) * 96,
        labelOffsetY: (position - (count - 1) / 2) * 44,
      },
    };
  });
}
