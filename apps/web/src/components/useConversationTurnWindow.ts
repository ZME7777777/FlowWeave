import { useCallback, useEffect, useRef, type RefObject } from 'react';

export interface ConversationTurnLayout {
  height: number;
  details: boolean[];
}

export type ConversationTurnLayouts = Map<string, ConversationTurnLayout>;
export type ObserveTurn = (element: HTMLElement, update: (visible: boolean) => void) => () => void;

/** One observer per transcript. Only measured turns may become placeholders. */
export function useConversationTurnWindow(viewport: RefObject<HTMLElement | null>, initialLayouts?: ConversationTurnLayouts) {
  const layouts = useRef(initialLayouts ?? new Map<string, ConversationTurnLayout>());
  const observer = useRef<IntersectionObserver | undefined>(undefined);
  const callbacks = useRef(new Map<Element, (visible: boolean) => void>());
  const observe = useCallback<ObserveTurn>((element, update) => {
    if (!viewport.current || typeof IntersectionObserver === 'undefined') return () => undefined;
    if (!observer.current) {
      observer.current = new IntersectionObserver(entries => {
        for (const entry of entries) callbacks.current.get(entry.target)?.(entry.isIntersecting);
      }, { root: viewport.current, rootMargin: '1200px 0px' });
    }
    callbacks.current.set(element, update);
    observer.current.observe(element);
    return () => {
      callbacks.current.delete(element);
      observer.current?.unobserve(element);
    };
  }, [viewport]);
  useEffect(() => () => {
    observer.current?.disconnect();
    observer.current = undefined;
  }, []);
  return { layouts: layouts.current, observe };
}
