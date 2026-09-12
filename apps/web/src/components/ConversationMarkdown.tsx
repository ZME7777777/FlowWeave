import { useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { deploymentBasePath } from '../deploymentPath';

function MarkdownImage({ src, alt, ...props }: ComponentPropsWithoutRef<'img'>) {
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
  return <img {...props} loading="lazy" decoding="async" className={`conversation-markdown-image${props.className ? ` ${props.className}` : ''}`} src={resolvedSource} alt={alt ?? ''} onError={() => setFailed(true)}/>;
}

function MarkdownLink({ href, ...props }: ComponentPropsWithoutRef<'a'>) {
  const isExternal = typeof href === 'string' && /^(?:https?:\/\/|mailto:)/i.test(href);
  return <a {...props} href={href} {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}/>;
}

export function ConversationMarkdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink, img: MarkdownImage }}>{children}</ReactMarkdown>;
}
