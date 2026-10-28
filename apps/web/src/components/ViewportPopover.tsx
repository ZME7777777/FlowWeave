import { forwardRef, useLayoutEffect, useRef, type HTMLAttributes } from 'react';
import { createPortal } from 'react-dom';

type AnchorBounds = Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom' | 'width'>;
type PopoverAnchor = { x: number; y: number } | (() => AnchorBounds | Element | null | undefined);
type Props = HTMLAttributes<HTMLDivElement> & {
  anchor: PopoverAnchor;
  align?: 'start' | 'end';
  placement?: 'top' | 'bottom';
  gap?: number;
  matchAnchorWidth?: boolean;
  heightLimit?: number;
};

/** Measure the real popover before paint; portals also escape clipping ancestors. */
export const ViewportPopover = forwardRef<HTMLDivElement, Props>(function ViewportPopover({ anchor, align = 'start', placement = 'bottom', gap = 4, matchAnchorWidth = false, heightLimit, style, children, ...props }, forwardedRef) {
  const popover = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = popover.current;
    if (!element) return;
    const update = () => {
      const target = typeof anchor === 'function' ? anchor() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y, width: 0 };
      if (!target) return;
      const bounds = target instanceof Element ? target.getBoundingClientRect() : target;
      const viewport = window.visualViewport;
      const margin = 8;
      const leftEdge = (viewport?.offsetLeft ?? 0) + margin;
      const topEdge = (viewport?.offsetTop ?? 0) + margin;
      const rightEdge = leftEdge + (viewport?.width ?? document.documentElement.clientWidth) - margin * 2;
      const bottomEdge = topEdge + (viewport?.height ?? window.innerHeight) - margin * 2;
      const maxWidth = Math.max(0, rightEdge - leftEdge);
      const maxHeight = Math.max(0, Math.min(bottomEdge - topEdge, heightLimit ?? Infinity));
      element.style.maxWidth = `${maxWidth}px`;
      if (matchAnchorWidth) element.style.width = `${Math.min(bounds.width, maxWidth)}px`;
      const width = element.getBoundingClientRect().width;
      // scrollHeight includes clipped content, so resizing a constrained menu can expand it again.
      const height = Math.min(element.scrollHeight + element.offsetHeight - element.clientHeight, maxHeight);
      const below = Math.max(0, bottomEdge - bounds.bottom - gap);
      const above = Math.max(0, bounds.top - gap - topEdge);
      const upward = placement === 'top' ? !(height > above && below > above) : height > below && above > below;
      const sideHeight = Math.min(maxHeight, upward ? above : below);
      // If even one action cannot fit beside a large/offscreen anchor, use the viewport.
      const availableHeight = sideHeight < Math.min(height, 32) ? maxHeight : sideHeight;
      element.style.maxHeight = `${availableHeight}px`;
      const visibleHeight = Math.min(height, availableHeight);
      let left = align === 'end' ? bounds.right - width : bounds.left;
      if (align === 'start' && left + width > rightEdge) left = bounds.right - width;
      if (align === 'end' && left < leftEdge) left = bounds.left;
      const top = upward ? bounds.top - gap - visibleHeight : bounds.bottom + gap;
      element.style.left = `${Math.max(leftEdge, Math.min(left, rightEdge - width))}px`;
      element.style.top = `${Math.max(topEdge, Math.min(top, bottomEdge - visibleHeight))}px`;
      element.style.visibility = 'visible';
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    const target = typeof anchor === 'function' ? anchor() : undefined;
    if (target instanceof Element) observer.observe(target);
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    window.visualViewport?.addEventListener('resize', update);
    window.visualViewport?.addEventListener('scroll', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
      window.visualViewport?.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('scroll', update);
    };
  }, [anchor, align, placement, gap, matchAnchorWidth, heightLimit]);
  return createPortal(<div {...props} data-viewport-popover="" ref={element => {
    popover.current = element;
    if (typeof forwardedRef === 'function') forwardedRef(element);
    else if (forwardedRef) forwardedRef.current = element;
  }} style={{ ...style, position: 'fixed', inset: 'auto', left: 0, top: 0, margin: 0, minWidth: 0, maxWidth: 'calc(100vw - 16px)', maxHeight: 'calc(100dvh - 16px)', overflow: 'auto', overscrollBehavior: 'contain', boxSizing: 'border-box', zIndex: 2000, visibility: 'hidden' }}>{children}</div>, document.body);
});
