import { useEffect, useState } from 'react';

export function RuntimeBrowserFrame({ source, title }: { source: string; title: string }) {
  const [result, setResult] = useState<{ source: string; html?: string; error?: string }>();
  useEffect(() => {
    const controller = new AbortController();
    // Fetch in the authenticated host. An opaque sandbox cannot send the host's
    // SameSite login cookie, even for its initial iframe navigation.
    void fetch(source, { credentials: 'include', signal: controller.signal })
      .then(async response => {
        const html = await response.text();
        if (!response.ok && !response.headers.get('content-type')?.includes('text/html')) {
          throw new Error('页面加载失败，请刷新后重试。');
        }
        const document = new DOMParser().parseFromString(html, 'text/html');
        const base = document.querySelector('base');
        if (base) base.href = new URL(base.getAttribute('href') ?? '/', source).href;
        else {
          const fallbackBase = document.createElement('base');
          fallbackBase.href = source;
          document.head.prepend(fallbackBase);
        }
        if (!controller.signal.aborted) {
          setResult({ source, html: `<!doctype html>${document.documentElement.outerHTML}` });
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setResult({ source, error: '页面加载失败，请刷新后重试。' });
      });
    return () => controller.abort();
  }, [source]);
  if (result?.source !== source) return <div className="agent-workspace-browser-empty" role="status">正在加载页面</div>;
  if (result.error) return <div className="agent-workspace-browser-empty" role="alert">{result.error}</div>;
  return <iframe className="agent-workspace-browser-frame" title={title} srcDoc={result.html} sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads" referrerPolicy="no-referrer"/>;
}
