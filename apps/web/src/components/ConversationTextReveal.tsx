import { useLayoutEffect, useMemo, useRef, useState } from 'react';

export function ConversationTextReveal({ children, reveal }: { children: string; reveal: boolean }) {
  const characters = useMemo(() => Array.from(children), [children]);
  const visibleLength = useRef(characters.length);
  const previousText = useRef<string | undefined>(undefined);
  const previousReveal = useRef(reveal);
  const [visible, setVisible] = useState(characters.length);

  useLayoutEffect(() => {
    const previous = previousText.current;
    previousText.current = children;
    const startsReveal = reveal && !previousReveal.current;
    previousReveal.current = reveal;
    if (!reveal || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      visibleLength.current = characters.length;
      setVisible(characters.length);
      return;
    }

    const initial = startsReveal ? 0 : previous && children.startsWith(previous)
      ? Math.min(visibleLength.current, characters.length)
      : 0;
    const addedCharacters = characters.length - initial;
    if (addedCharacters <= 0) return;

    let frame: number | undefined;
    const startedAt = performance.now();
    const duration = Math.min(3_600, Math.max(1_400, 650 + addedCharacters * 14));
    visibleLength.current = initial;
    setVisible(initial);

    const advance = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const easedProgress = progress ** 1.25;
      const next = initial + Math.round(addedCharacters * easedProgress);
      if (next !== visibleLength.current) {
        visibleLength.current = next;
        setVisible(next);
      }
      if (progress < 1) frame = window.requestAnimationFrame(advance);
    };

    frame = window.requestAnimationFrame(advance);
    return () => { if (frame !== undefined) window.cancelAnimationFrame(frame); };
  }, [characters.length, children, reveal]);

  if (!reveal) return <span className="conversation-text-reveal" aria-label={children}>{children}</span>;
  const tailStart = Math.max(0, visible - 18);
  const stableText = characters.slice(0, tailStart).join('');
  const tail = characters.slice(tailStart, visible);
  return <span className="conversation-text-reveal" aria-label={children}>{stableText}{tail.map((character, index) => <span className="conversation-text-reveal-tail" key={tailStart + index}>{character}</span>)}</span>;
}
