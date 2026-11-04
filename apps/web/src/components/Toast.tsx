import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

export const TOAST_DURATION_MS = 2000;

type ToastProps = {
  message: string;
  kind?: 'success' | 'error';
  /** Called after the display duration so the owner clears its message state. */
  onDismiss: () => void;
};

/** Transient notice: centered at the top, sized to its text, auto-dismissed after 2s. */
export function Toast({ message, kind = 'success', onDismiss }: ToastProps) {
  const dismiss = useRef(onDismiss);
  dismiss.current = onDismiss;
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => dismiss.current(), TOAST_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [message]);
  if (!message) return null;
  return createPortal(
    <div className={`toast toast-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>{message}</div>,
    document.body
  );
}
