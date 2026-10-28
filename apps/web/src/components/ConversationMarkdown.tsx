import { createContext, isValidElement, useContext, useEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import 'katex/dist/katex.min.css';
import rehypeKatex from 'rehype-katex';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { deploymentBasePath } from '../deploymentPath';
import { MarkdownCodeBlock, MermaidDiagram } from './MermaidDiagram';
import { ConversationTextReveal } from './ConversationTextReveal';
import { WebsiteIcon } from './WebsiteIcon';
import { isMermaidDiagram, markdownCodeText, normalizeNestedMarkdownFences } from './markdownCodeBlock';
import { CONVERSATION_MARKDOWN_SYNTAX } from './conversationSearch';

interface MarkdownRenderOptions {
  onOpenWorkspaceFile?: (href: string) => boolean;
  onOpenBrowser?: (href: string) => boolean;
  onOpenImage?: (src: string, alt?: string) => void;
  workspaceImageUrl?: (href: string) => string | undefined;
}

const MarkdownRenderOptionsContext = createContext<MarkdownRenderOptions>({});

function MarkdownImage({ src, alt, onOpenImage: imageOpenHandler, ...props }: ComponentPropsWithoutRef<'img'> & { onOpenImage?: (src: string, alt?: string) => void }) {
  const { onOpenImage } = useContext(MarkdownRenderOptionsContext);
  const openImage = imageOpenHandler ?? onOpenImage;
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
  return openImage && typeof resolvedSource === 'string'
    ? <button type="button" className="conversation-markdown-image-button" aria-label={`预览图片：${alt || '会话图片'}`} onClick={() => openImage(resolvedSource, alt ?? undefined)}>{image}</button>
    : image;
}

function workspaceImageLink(href: string): boolean {
  if (/\.(?:avif|gif|jpe?g|png|svg|webp)(?:[?#].*)?$/i.test(href)) return true;
  try {
    const url = new URL(href, window.location.origin);
    return url.origin === window.location.origin
      && url.pathname.startsWith('/api/v1/')
      && url.pathname.endsWith('/workspace/file')
      && /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(url.searchParams.get('path') ?? '');
  } catch {
    return false;
  }
}

function MarkdownLink({ href, onClick, children, ...props }: ComponentPropsWithoutRef<'a'>) {
  const { onOpenWorkspaceFile, onOpenBrowser, onOpenImage, workspaceImageUrl } = useContext(MarkdownRenderOptionsContext);
  const isExternal = typeof href === 'string' && /^(?:https?:\/\/|mailto:)/i.test(href);
  const isWebsite = typeof href === 'string' && /^https?:\/\//i.test(href);
  const imageUrl = typeof href === 'string' && workspaceImageLink(href)
    ? workspaceImageUrl?.(href)
    : undefined;
  const label = typeof children === 'string' ? children : undefined;
  if (imageUrl) return <MarkdownImage src={imageUrl} alt={label} onOpenImage={() => onOpenImage?.(href!, label)} />;
  return <a {...props} href={href} {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented || !href) return;
    if (onOpenWorkspaceFile?.(href) || (isWebsite && onOpenBrowser?.(href))) {
      event.preventDefault();
      return;
    }
  }}>{isWebsite && href ? <WebsiteIcon url={href} className="conversation-link-icon"/> : null}{children}</a>;
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

type FormulaSpanProps = ComponentPropsWithoutRef<'span'> & {
  node?: unknown;
  'data-formula-source'?: string;
  'data-formula-display'?: string | boolean;
};

async function copyFormula(source: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(source);
      return;
    } catch {
      // Permission-restricted embedded browsers can still permit this fallback.
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = source;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('Clipboard is unavailable');
}

function MarkdownSpan({ children, node: _node, 'data-formula-source': source, 'data-formula-display': displayValue, ...props }: FormulaSpanProps) {
  void _node;
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copyTimer = useRef<number | undefined>(undefined);
  const display = displayValue === true || displayValue === 'true';

  useEffect(() => () => {
    if (copyTimer.current !== undefined) window.clearTimeout(copyTimer.current);
  }, []);

  if (!source) return <span {...props}>{children}</span>;

  const markdown = display ? `$$\n${source}\n$$` : `$${source}$`;
  const copied = copyState === 'copied';
  const copySource = () => {
    void copyFormula(markdown).then(() => {
      setCopyState('copied');
      if (copyTimer.current !== undefined) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopyState('idle'), 1_800);
    }).catch(() => setCopyState('failed'));
  };

  return <span {...props} className={`conversation-formula${props.className ? ` ${props.className}` : ''}`} data-formula-display={display}>
    {children}
    <button type="button" className={`conversation-formula-copy${copyState === 'failed' ? ' failed' : ''}`} aria-label={copied ? '公式已复制' : copyState === 'failed' ? '复制公式失败，请重试' : '复制公式'} title={copied ? '已复制' : copyState === 'failed' ? '复制失败，请重试' : '复制公式'} onClick={copySource}>
      {copied ? <Check size={14} strokeWidth={2.4} aria-hidden="true"/> : <Copy size={14} aria-hidden="true"/>}
    </button>
  </span>;
}

type HastNode = { type?: unknown; tagName?: unknown; properties?: Record<string, unknown>; children?: HastNode[]; value?: unknown };

function hasClass(node: HastNode, name: string): boolean {
  const className = node.properties?.className;
  return Array.isArray(className) ? className.includes(name) : className === name;
}

function texAnnotationValue(node: HastNode): string | undefined {
  if (node.type === 'element' && node.tagName === 'annotation' && node.properties?.encoding === 'application/x-tex') {
    return node.children?.map(child => typeof child.value === 'string' ? child.value : '').join('');
  }
  for (const child of node.children ?? []) {
    const value = texAnnotationValue(child);
    if (value !== undefined) return value;
  }
  return undefined;
}

function rehypeFormulaMetadata() {
  return (tree: HastNode) => {
    const visit = (node: HastNode) => {
      if (node.type === 'element' && hasClass(node, 'katex-display')) {
        const source = texAnnotationValue(node);
        if (source !== undefined) node.properties = { ...node.properties, dataFormulaSource: source, dataFormulaDisplay: 'true' };
        return;
      }
      if (node.type === 'element' && hasClass(node, 'katex')) {
        const source = texAnnotationValue(node);
        if (source !== undefined) node.properties = { ...node.properties, dataFormulaSource: source, dataFormulaDisplay: 'false' };
        return;
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

export function ConversationMarkdown({ children, reveal = false, onOpenWorkspaceFile, onOpenBrowser, onOpenImage, workspaceImageUrl }: { children: string; reveal?: boolean; onOpenWorkspaceFile?: (href: string) => boolean; onOpenBrowser?: (href: string) => boolean; onOpenImage?: (src: string, alt?: string) => void; workspaceImageUrl?: (href: string) => string | undefined }) {
  const markdown = useMemo(() => normalizeNestedMarkdownFences(children), [children]);
  const renderOptions = useMemo(() => ({ onOpenWorkspaceFile, onOpenBrowser, onOpenImage, workspaceImageUrl }), [onOpenWorkspaceFile, onOpenBrowser, onOpenImage, workspaceImageUrl]);
  if (!CONVERSATION_MARKDOWN_SYNTAX.test(markdown)) return <p><ConversationTextReveal reveal={reveal}>{markdown}</ConversationTextReveal></p>;
  return <MarkdownRenderOptionsContext value={renderOptions}>
    <div className={reveal ? 'conversation-text-reveal-block conversation-text-reveal-shimmer' : undefined}>
      <ReactMarkdown urlTransform={(url, key) => (key === 'src' ? workspaceImageUrl?.(url) : undefined) ?? defaultUrlTransform(url)} remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex, rehypeFormulaMetadata]} components={{ a: MarkdownLink, img: MarkdownImage, pre: MarkdownPre, span: MarkdownSpan, table: MarkdownTable }}>{markdown}</ReactMarkdown>
    </div>
  </MarkdownRenderOptionsContext>;
}
