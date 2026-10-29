import { Activity, Suspense, useLayoutEffect, useRef, useState, type ReactNode } from 'react';

type PageEntry = { group: string; content: ReactNode };

function PageFrame({ pageKey, children }: { pageKey: string; children: ReactNode }) {
  const root = useRef<HTMLDivElement>(null);
  const scroll = useRef<Array<{ element: Element; top: number; left: number }>>([]);
  useLayoutEffect(() => {
    const element = root.current;
    const restore = () => scroll.current.forEach(item => {
      if (element?.contains(item.element)) {
        item.element.scrollTop = item.top;
        item.element.scrollLeft = item.left;
      }
    });
    restore();
    const frame = window.requestAnimationFrame(restore);
    return () => {
      window.cancelAnimationFrame(frame);
      if (element) scroll.current = [element, ...element.querySelectorAll('*')]
        .filter(item => item.scrollTop || item.scrollLeft)
        .map(item => ({ element: item, top: item.scrollTop, left: item.scrollLeft }));
    };
  }, []);
  return <div ref={root} data-page-tab={pageKey}>{children}</div>;
}

/** Keep visited tab state while React disconnects hidden subscriptions and effects. */
export function RetainedPages({ activeKey, group, retainedGroups, children, fallback }: {
  activeKey: string; group: string; retainedGroups: string[]; children: ReactNode; fallback: ReactNode;
}) {
  const [saved, setSaved] = useState<Map<string, PageEntry>>(() => new Map());
  useLayoutEffect(() => {
    setSaved(current => {
      const allowed = new Set([...retainedGroups, group]);
      const next = new Map([...current].filter(([, entry]) => allowed.has(entry.group)));
      next.set(activeKey, { group, content: children });
      if (next.size === current.size && [...next].every(([key, entry]) => entry.group === current.get(key)?.group && entry.content === current.get(key)?.content)) return current;
      return next;
    });
  }, [activeKey, children, group, retainedGroups]);
  const entries = new Map([...saved].filter(([, entry]) => retainedGroups.includes(entry.group) || entry.group === group));
  entries.set(activeKey, { group, content: children });
  return <>{[...entries].map(([key, entry]) => <Activity key={key} mode={key === activeKey ? 'visible' : 'hidden'}><Suspense fallback={fallback}><PageFrame pageKey={key}>{entry.content}</PageFrame></Suspense></Activity>)}</>;
}
