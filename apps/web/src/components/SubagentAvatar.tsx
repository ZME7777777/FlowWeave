import { Boxes, CircleDot, GitBranch, Layers3, Search, Square, type LucideIcon } from 'lucide-react';
import type { SubagentAvatarSlot } from '../utils/subagentAvatar';
import '../utils/subagent-avatar.css';

const avatarIcons: Record<SubagentAvatarSlot, LucideIcon> = {
  orbit: CircleDot,
  branch: GitBranch,
  grid: Boxes,
  spark: Layers3,
  search: Search,
  terminal: Square,
};

export function SubagentAvatar({
  slot, status, size = 15,
}: {
  slot: SubagentAvatarSlot;
  status: 'running' | 'completed' | 'error';
  size?: number;
}) {
  const Icon = avatarIcons[slot];
  return <span className={`subagent-avatar ${slot} ${status}`} aria-hidden="true"><Icon size={size}/></span>;
}
