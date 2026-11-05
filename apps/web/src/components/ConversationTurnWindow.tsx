import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import type { ConversationTurnLayouts, ObserveTurn } from './useConversationTurnWindow';

function nativeDetails(element: HTMLElement) {
  return [...element.querySelectorAll<HTMLDetailsElement>('details')]
    .filter(detail => !detail.matches('.conversation-process-summary, .conversation-activity-group'));
}

export function ConversationTurnWindow({ turnId, layoutKey, layouts, observe, viewport, pinned, children }: {
  turnId: string;
  layoutKey: string;
  layouts: ConversationTurnLayouts;
  observe: ObserveTurn;
  viewport: RefObject<HTMLElement | null>;
  pinned: boolean;
  children: () => ReactNode;
}) {
  const element = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(() => !layouts.has(layoutKey));
  const mounted = visible || pinned;
  const mountedRef = useRef(mounted);
  const pinnedRef = useRef(pinned);
  mountedRef.current = mounted;
  pinnedRef.current = pinned;
  const wasMounted = useRef(false);

  const measure = useCallback(() => {
    const turn = element.current;
    const root = viewport.current;
    if (!turn || !root || !mountedRef.current) return;
    const bounds = turn.getBoundingClientRect();
    // A retained Tab can be hidden with display:none. Do not erase its height.
    if (!bounds.height || !turn.getClientRects().length) return;
    const previous = layouts.get(layoutKey);
    layouts.set(layoutKey, { height: bounds.height, details: previous?.details ?? [] });
    // Remeasuring a previously visited turn above the viewport (e.g. after a
    // width change) must preserve the reader's current position.
    if (previous && bounds.bottom <= root.getBoundingClientRect().top) {
      const delta = bounds.height - previous.height;
      if (Math.abs(delta) > 0.5) root.scrollTop += delta;
    }
  }, [layoutKey, layouts, viewport]);

  useLayoutEffect(() => {
    if (mounted && !wasMounted.current && element.current) {
      const saved = layouts.get(layoutKey)?.details;
      if (saved) nativeDetails(element.current).forEach((detail, index) => { detail.open = saved[index] ?? false; });
    }
    wasMounted.current = mounted;
    if (mounted) measure();
  });
  useLayoutEffect(() => () => {
    const turn = element.current;
    const layout = layouts.get(layoutKey);
    if (turn && layout && mountedRef.current) layout.details = nativeDetails(turn).map(detail => detail.open);
  }, [layoutKey, layouts]);
  useEffect(() => {
    const turn = element.current;
    if (!turn) return;
    return observe(turn, nearViewport => {
      if (nearViewport) {
        setVisible(true);
        return;
      }
      if (!mountedRef.current || pinnedRef.current) return;
      // Keep a focused editor/control or an active text selection alive.
      const selection = window.getSelection();
      if (turn.contains(document.activeElement)
        || (selection && !selection.isCollapsed && selection.containsNode(turn, true))) return;
      measure();
      const layout = layouts.get(layoutKey);
      if (!layout) return;
      layout.details = nativeDetails(turn).map(detail => detail.open);
      setVisible(false);
    });
  }, [layoutKey, layouts, measure, observe, pinned]);
  useEffect(() => {
    if (!mounted || !element.current || typeof ResizeObserver === 'undefined') return;
    const resize = new ResizeObserver(measure);
    resize.observe(element.current);
    return () => resize.disconnect();
  }, [measure, mounted]);

  return <section ref={element} className="conversation-turn" data-conversation-turn={turnId}
    data-virtualized={mounted ? undefined : 'true'}
    style={mounted ? undefined : { height: layouts.get(layoutKey)?.height }}>
    {mounted && children()}
  </section>;
}
