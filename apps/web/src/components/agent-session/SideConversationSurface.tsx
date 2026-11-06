import { CircleAlert, LoaderCircle } from 'lucide-react';
import type { ComponentProps, ReactNode } from 'react';
import { ConversationSurface } from '../ConversationSurface';

interface SideConversationSurfaceProps {
  label: string;
  loading: boolean;
  conversation?: ComponentProps<typeof ConversationSurface>;
  empty?: ReactNode;
  notice?: ReactNode;
  error?: string;
  composer?: ReactNode;
}

/** Shared layout only. Creation, retention, deletion and TTL belong to the caller. */
export function SideConversationSurface({ label, loading, conversation, empty, notice, error, composer }: SideConversationSurfaceProps) {
  return <section className="agent-sidebar-conversation" aria-label={label}>
    <div className="agent-sidebar-chat-content">
      {loading ? <div className="conversation-surface-empty" role="status"><LoaderCircle className="conversation-activity-spin" size={16}/><b>正在加载会话</b></div>
        : conversation ? <ConversationSurface {...conversation}/> : empty}
      {notice}
      {error && <section className="agent-workbench-error" role="alert"><CircleAlert size={17}/><div><b>操作未完成</b><span>{error}</span></div></section>}
    </div>
    {composer}
  </section>;
}
