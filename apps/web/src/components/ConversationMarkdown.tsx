import { isValidElement, useLayoutEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { deploymentBasePath } from '../deploymentPath';
import { MarkdownCodeBlock, MermaidDiagram } from './MermaidDiagram';
import { isMermaidDiagram, markdownCodeText, normalizeNestedMarkdownFences } from './markdownCodeBlock';

const MARKDOWN_SYNTAX = /(^|\n)\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)|[`*_~]|!?\[[^\]]*\]\([^)]*\)|\|/;

function PlainTextReveal({ children, reveal }: { children: string; reveal: boolean }) {
  const characters = useMemo(() => Array.from(children), [children]);
  const visibleLength = useRef(0);
  const previousText = useRef<string | undefined>(undefined);
  const revealStarted = useRef(reveal);
  const [visible, setVisible] = useState(() => reveal ? 0 : characters.length);

  useLayoutEffect(() => {
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const previous = previousText.current;
    previousText.current = children;
    const startsReveal = reveal && !revealStarted.current;
    if (startsReveal) revealStarted.current = true;
    if (reducedMotion || (!reveal && !revealStarted.current)) {
      visibleLength.current = characters.length;
      setVisible(characters.length);
      return;
    }

    let current = startsReveal ? 0 : previous && children.startsWith(previous)
      ? Math.min(visibleLength.current, characters.length)
      : 0;
    let frame: number | undefined;
    const startedAt = performance.now();
    let previousFrame = startedAt;
    const maximumDuration = Math.min(4_800, Math.max(1_600, 900 + characters.length * 14));
    let nextStepAt = startedAt;
    visibleLength.current = current;
    setVisible(current);

    const advance = (now: number) => {
      const elapsed = now - startedAt;
      const delta = now - previousFrame;
      previousFrame = now;
      const remaining = characters.length - current;
      const progress = Math.min(1, elapsed / maximumDuration);
      const charactersPerSecond = Math.min(180, 10 + progress * 95 + remaining * 0.035);
      if (now >= nextStepAt) {
        const chunkSize = Math.min(remaining, Math.max(1, Math.round(charactersPerSecond * Math.max(delta, 90) / 1_000)));
        current += chunkSize;
        visibleLength.current = current;
        setVisible(current);
        nextStepAt = now + Math.max(45, 150 - progress * 80);
      }
      if (current < characters.length && elapsed < maximumDuration) {
        frame = window.requestAnimationFrame(advance);
      } else if (current < characters.length) {
        visibleLength.current = characters.length;
        setVisible(characters.length);
      }
    };

    frame = window.requestAnimationFrame(advance);
    return () => { if (frame !== undefined) window.cancelAnimationFrame(frame); };
  }, [characters.length, children, reveal]);

  const revealing = visible < characters.length;
  return <span className="conversation-text-reveal" data-reveal={revealStarted.current || undefined} data-revealing={revealing || undefined} aria-label={children}>{characters.slice(0, visible).join('')}</span>;
}

function MarkdownImage({ src, alt, onOpenImage, ...props }: ComponentPropsWithoutRef<'img'> & { onOpenImage?: (src: string, alt?: string) => void }) {
  const [failed, setFailed] = useState(false);
  // Runtime message projection deliberately produces API-root paths so the
  // backend does not need to know where the web app is mounted.
  const resolvedSource = typeof src === 'string' && src.startsWith('/api/v1/')
    ? `${import.meta.env.VITE_API_BASE_URL || deploymentBasePath}${src}`
    : src;
  const safeSource = typeof resolvedSource === 'string' && /^(?:https?:|data:image\/|blob:|\/)/i.test(resolvedSource);
  if (!safeSource || failed) {
    return <span className="conversation-image-unavailable" role="status">
      <b>{alt || '图片无法显示'}</b>
      {safeSource
        ? <a href={resolvedSource} target="_blank" rel="noreferrer">在新窗口打开图片</a>
        : <small>图片地址无效</small>}
    </span>;
  }
  const image = <img {...props} loading="lazy" decoding="async" className={`conversation-markdown-image${props.className ? ` ${props.className}` : ''}`} src={resolvedSource} alt={alt ?? ''} onError={() => setFailed(true)}/>;
  return onOpenImage && typeof resolvedSource === 'string'
    ? <button type="button" className="conversation-markdown-image-button" aria-label={`预览图片：${alt || '会话图片'}`} onClick={() => onOpenImage(resolvedSource, alt ?? undefined)}>{image}</button>
    : image;
}

function MarkdownLink({ href, onClick, onOpenWorkspaceFile, ...props }: ComponentPropsWithoutRef<'a'> & {
  onOpenWorkspaceFile?: (href: string) => boolean;
}) {
  const isExternal = typeof href === 'string' && /^(?:https?:\/\/|mailto:)/i.test(href);
  return <a {...props} href={href} {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented || !href) return;
    if (onOpenWorkspaceFile?.(href)) {
      event.preventDefault();
      return;
    }
  }}/>;
}

function MarkdownPre({ children, node: _node, ...props }: ComponentPropsWithoutRef<'pre'> & { node?: unknown }) {
  void _node;
  let source = '';
  if (isValidElement(children) && children.type === 'code') {
    const code = children.props as { className?: string; children?: ReactNode };
    source = markdownCodeText(code.children).replace(/\n$/, '');
    if (source && isMermaidDiagram(code.className, source)) return <MermaidDiagram source={source}/>;
  }
  return <MarkdownCodeBlock {...props} source={source}>{children}</MarkdownCodeBlock>;
}

function MarkdownTable({ children, node: _node, ...props }: ComponentPropsWithoutRef<'table'> & { node?: unknown }) {
  void _node;
  return <div className="conversation-markdown-table-scroll"><table {...props}>{children}</table></div>;
}

export function ConversationMarkdown({ children, reveal = false, onOpenWorkspaceFile, onOpenImage }: { children: string; reveal?: boolean; onOpenWorkspaceFile?: (href: string) => boolean; onOpenImage?: (src: string, alt?: string) => void }) {
  const markdown = useMemo(() => normalizeNestedMarkdownFences(children), [children]);
  if (!MARKDOWN_SYNTAX.test(markdown)) return <p><PlainTextReveal reveal={reveal}>{markdown}</PlainTextReveal></p>;
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: props => <MarkdownLink {...props} onOpenWorkspaceFile={onOpenWorkspaceFile}/>,
    img: props => <MarkdownImage {...props} onOpenImage={onOpenImage}/>, pre: MarkdownPre, table: MarkdownTable,
  }}>{markdown}</ReactMarkdown>;
}
