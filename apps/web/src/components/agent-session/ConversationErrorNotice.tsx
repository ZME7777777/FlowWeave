import { CircleAlert, RefreshCw, X } from 'lucide-react';
import { useEffect, useState } from 'react';

interface ConversationErrorNoticeProps {
  error: Error;
  onDismiss: (error: Error) => void;
  onRetry?: () => void;
  transient?: boolean;
}

export function ConversationErrorNotice({ error, onDismiss, onRetry, transient = false }: ConversationErrorNoticeProps) {
  const [dismissedError, setDismissedError] = useState<Error>();
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!transient || hovered || focused || dismissedError === error) return;
    const timer = window.setTimeout(() => {
      setDismissedError(error);
      onDismiss(error);
    }, 8_000);
    return () => window.clearTimeout(timer);
  }, [dismissedError, error, focused, hovered, onDismiss, transient]);

  if (dismissedError === error) return null;
  return <section
    className="agent-conversation-notice"
    role="alert"
    onMouseEnter={() => setHovered(true)}
    onMouseLeave={() => setHovered(false)}
    onFocusCapture={() => setFocused(true)}
    onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }}
  >
    <CircleAlert size={14} aria-hidden="true"/>
    <span>{onRetry && '会话暂时无法读取：'}{error.message}</span>
    {onRetry && <button type="button" className="agent-conversation-notice-retry" onClick={onRetry}><RefreshCw size={12}/>重新读取会话</button>}
    <button type="button" className="agent-conversation-notice-close" aria-label="关闭错误提示" onClick={() => { setDismissedError(error); onDismiss(error); }}><X size={13}/></button>
  </section>;
}
