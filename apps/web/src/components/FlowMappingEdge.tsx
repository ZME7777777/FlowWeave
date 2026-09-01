import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from '@xyflow/react';

export type FlowMappingEdgeData = {
  label: string;
  labelFraction: number;
  labelLane: number;
};

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

// React Flow consumes this registry outside React render. Keeping it beside
// its node component avoids a circular dependency in every graph consumer.
// eslint-disable-next-line react-refresh/only-export-components
export const flowMappingEdgeTypes = { mappingEdge: FlowMappingEdge };

/**
 * Place every mapping between a node pair in a stable grid. Three columns keep
 * labels apart on ordinary canvases; additional mappings occupy parallel rows.
 * This scales beyond the two-edge case without placing every label at one path
 * midpoint.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function withMappingLabelOffsets(edges: Edge[]): Array<Edge<FlowMappingEdgeData>> {
  const key = (edge: Edge) => `${edge.source}→${edge.target}`;
  const groups = new Map<string, Edge[]>();
  edges.forEach(edge => groups.set(key(edge), [...(groups.get(key(edge)) ?? []), edge]));
  return edges.map(edge => {
    const group = groups.get(key(edge)) ?? [edge];
    const ordered = [...group].sort((left, right) =>
      `${left.sourceHandle ?? ''}:${left.targetHandle ?? ''}:${left.id}`
        .localeCompare(`${right.sourceHandle ?? ''}:${right.targetHandle ?? ''}:${right.id}`),
    );
    const position = ordered.findIndex(item => item.id === edge.id);
    const columns = Math.min(3, ordered.length);
    const rows = Math.ceil(ordered.length / columns);
    const column = position % columns;
    const row = Math.floor(position / columns);
    const label = typeof edge.label === 'string'
      ? edge.label
      : `${edge.sourceHandle?.replace('output:', '') ?? ''} → ${edge.targetHandle?.replace('input:', '') ?? ''}`;
    return {
      ...edge,
      type: 'mappingEdge',
      label: undefined,
      data: {
        label,
        // Reserve the outer path sections for labels and split each group into
        // three columns. Rows are offset from the span on its perpendicular.
        labelFraction: 0.22 + (0.56 * (column + 0.5)) / columns,
        labelLane: (row - (rows - 1) / 2) * 34,
      },
    };
  });
}
