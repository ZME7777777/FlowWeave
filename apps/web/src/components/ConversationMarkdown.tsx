import { isValidElement, useMemo, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { deploymentBasePath } from '../deploymentPath';
import { MarkdownCodeBlock, MermaidDiagram } from './MermaidDiagram';
import { isMermaidDiagram, markdownCodeText, normalizeNestedMarkdownFences } from './markdownCodeBlock';

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
    if (event.defaultPrevented || !href || isExternal || !onOpenWorkspaceFile?.(href)) return;
    event.preventDefault();
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

export function ConversationMarkdown({ children, onOpenWorkspaceFile, onOpenImage }: { children: string; onOpenWorkspaceFile?: (href: string) => boolean; onOpenImage?: (src: string, alt?: string) => void }) {
  const markdown = useMemo(() => normalizeNestedMarkdownFences(children), [children]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: props => <MarkdownLink {...props} onOpenWorkspaceFile={onOpenWorkspaceFile}/>,
    img: props => <MarkdownImage {...props} onOpenImage={onOpenImage}/>, pre: MarkdownPre, table: MarkdownTable,
  }}>{markdown}</ReactMarkdown>;
}
