import { useEffect, useRef } from 'react';

interface EscapeEntry { id: number; close: () => void }
const entries: EscapeEntry[] = [];
let nextId = 1;
let listening = false;

function onKeyDown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.defaultPrevented) return;
  const top = entries.at(-1);
  if (!top) return;
  event.preventDefault();
  event.stopPropagation();
  top.close();
}

function syncListener() {
  if (entries.length && !listening) {
    document.addEventListener('keydown', onKeyDown, true);
    listening = true;
  } else if (!entries.length && listening) {
    document.removeEventListener('keydown', onKeyDown, true);
    listening = false;
  }
}

export function useEscapeClose(onClose: () => void, enabled = true) {
  const callback = useRef(onClose);
  callback.current = onClose;
  useEffect(() => {
    if (!enabled) return;
    const entry: EscapeEntry = { id: nextId++, close: () => callback.current() };
    entries.push(entry);
    syncListener();
    return () => {
      const index = entries.findIndex(item => item.id === entry.id);
      if (index >= 0) entries.splice(index, 1);
      syncListener();
    };
  }, [enabled]);
}
