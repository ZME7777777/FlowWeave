import { useEffect, useRef, type KeyboardEvent } from 'react';

export function useDialogFocus() {
  const dialogRef = useRef<HTMLElement>(null);
  const opener = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>('button:not(:disabled)')?.focus({ preventScroll: true });
    const previous = opener.current;
    return () => { if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  const onDialogKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab' || event.defaultPrevented) return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')]
      .filter(element => element.getClientRects().length > 0);
    const first = controls[0]; const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  };
  return { dialogRef, onDialogKeyDown };
}
