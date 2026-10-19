export function ConversationTextReveal({ children, reveal }: { children: string; reveal: boolean }) {
  return <span
    className={`conversation-text-reveal${reveal ? ' conversation-text-reveal-block conversation-text-reveal-shimmer' : ''}`}
    aria-label={children}
  >{children}</span>;
}
