import { isValidElement, useEffect, useId, useLayoutEffect, useRef, useState, type ComponentPropsWithoutRef, type PointerEvent as ReactPointerEvent, type ReactNode, type WheelEvent as ReactWheelEvent } from 'react';
import { createPortal } from 'react-dom';
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

function codeText(children: ReactNode): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(codeText).join('');
  return '';
}

function isMermaidDiagram(className: string | undefined, source: string): boolean {
  return /(?:^|\s)language-mermaid(?:\s|$)/.test(className ?? '')
    || /^(?:sequenceDiagram|flowchart|graph|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|gitGraph|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic)\b/m.test(source.trim());
}

async function copyDiagramSource(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Permission-restricted embedded browsers can still permit this fallback.
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('Clipboard is unavailable');
}

function MermaidDiagram({ source }: { source: string }) {
  const diagramId = useId().replace(/[^a-z0-9]/gi, '');
  const [mode, setMode] = useState<'image' | 'text'>('image');
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [zoom, setZoom] = useState(100);
  const [fitScale, setFitScale] = useState(1);
  const [diagramSize, setDiagramSize] = useState<{ width: number; height: number }>();
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [svg, setSvg] = useState('');
  const [rendering, setRendering] = useState(true);
  const [error, setError] = useState('');
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copyTimer = useRef<number | undefined>(undefined);
  const fullscreenCanvasRef = useRef<HTMLDivElement>(null);
  const dragStart = useRef<{ x: number; y: number; panX: number; panY: number } | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setRendering(true);
    setError('');
    setSvg('');
    void import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'base',
        themeVariables: {
          primaryColor: '#f4f8f5',
          primaryBorderColor: '#669278',
          primaryTextColor: '#26372d',
          lineColor: '#547361',
          secondaryColor: '#f8faf8',
          tertiaryColor: '#eef5ef',
          fontFamily: 'DM Mono, ui-monospace, monospace',
        },
      });
      return mermaid.render(`flowweave-mermaid-${diagramId}`, source);
    }).then(result => {
      if (!cancelled) setSvg(result.svg);
    }).catch(reason => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '图表语法无法渲染');
    }).finally(() => {
      if (!cancelled) setRendering(false);
    });
    return () => { cancelled = true; };
  }, [diagramId, source]);

  useEffect(() => () => {
    if (copyTimer.current !== undefined) window.clearTimeout(copyTimer.current);
  }, []);

  useEffect(() => {
    if (!isFullscreen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsFullscreen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [isFullscreen]);

  useLayoutEffect(() => {
    if (!isFullscreen || !svg) return;
    const canvas = fullscreenCanvasRef.current;
    const diagram = canvas?.querySelector('svg') as SVGSVGElement | null;
    if (!canvas || !diagram) return;
    const updateFitScale = () => {
      const width = diagram.viewBox.baseVal.width || diagram.width.baseVal.value;
      const height = diagram.viewBox.baseVal.height || diagram.height.baseVal.value;
      if (!width || !height) return;
      setDiagramSize({ width, height });
      setFitScale(Math.min((canvas.clientWidth - 64) / width, (canvas.clientHeight - 64) / height));
    };
    updateFitScale();
    const observer = new ResizeObserver(updateFitScale);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [isFullscreen, svg]);

  const copySource = () => {
    void copyDiagramSource(source).then(() => {
      setCopyState('copied');
      if (copyTimer.current !== undefined) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopyState('idle'), 1800);
    }).catch(() => setCopyState('failed'));
  };

  const openFullscreen = () => {
    setZoom(100);
    setPan({ x: 0, y: 0 });
    setIsFullscreen(true);
  };
  const closeFullscreen = () => setIsFullscreen(false);
  const changeZoom = (amount: number) => setZoom(current => Math.max(50, Math.min(500, current + amount)));
  const resetView = () => {
    setZoom(100);
    setPan({ x: 0, y: 0 });
  };
  const startDragging = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    dragStart.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };
  const dragDiagram = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start) return;
    setPan({ x: start.panX + event.clientX - start.x, y: start.panY + event.clientY - start.y });
  };
  const stopDragging = () => {
    dragStart.current = undefined;
    setDragging(false);
  };
  const zoomWithWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    changeZoom(event.deltaY < 0 ? 10 : -10);
  };
  const displayScale = fitScale * zoom / 100;

  const fullscreenPreview = isFullscreen && svg && createPortal(
    <div className="conversation-mermaid-fullscreen-backdrop" role="presentation" onMouseDown={event => {
      if (event.target === event.currentTarget) closeFullscreen();
    }}>
      <section className="conversation-mermaid-fullscreen" role="dialog" aria-modal="true" aria-label="Mermaid 图表全屏预览">
        <header>
          <b>全屏预览</b>
          <div className="conversation-mermaid-zoom-controls" role="group" aria-label="图表缩放">
            <button type="button" aria-label="缩小图表" onClick={() => changeZoom(-25)} disabled={zoom <= 50}>−</button>
            <output aria-live="polite">{zoom}%</output>
            <button type="button" aria-label="放大图表" onClick={() => changeZoom(25)} disabled={zoom >= 500}>+</button>
            <button type="button" onClick={resetView} disabled={zoom === 100 && pan.x === 0 && pan.y === 0}>复位</button>
            <button type="button" className="conversation-mermaid-fullscreen-close" onClick={closeFullscreen}>关闭</button>
          </div>
        </header>
        <div ref={fullscreenCanvasRef} className={`conversation-mermaid-fullscreen-canvas${dragging ? ' dragging' : ''}`} onWheel={zoomWithWheel} onPointerDown={startDragging} onPointerMove={dragDiagram} onPointerUp={stopDragging} onPointerCancel={stopDragging}>
          {diagramSize && <div className="conversation-mermaid-fullscreen-stage" style={{ width: `${diagramSize.width * displayScale}px`, height: `${diagramSize.height * displayScale}px`, transform: `translate(${pan.x}px, ${pan.y}px)` }}>
            <div className="conversation-mermaid-svg" role="img" aria-label="Mermaid 图表" style={{ transform: `scale(${displayScale})` }} dangerouslySetInnerHTML={{ __html: svg }}/>
          </div>}
          {!diagramSize && <div className="conversation-mermaid-svg conversation-mermaid-fullscreen-measure" aria-hidden="true" dangerouslySetInnerHTML={{ __html: svg }}/>}
          <span className="conversation-mermaid-fullscreen-hint">滚轮缩放 · 左键拖动</span>
        </div>
      </section>
    </div>,
    document.body,
  );

  return <section className="conversation-mermaid" aria-label="Mermaid 图表">
    <header className="conversation-mermaid-header">
      <span>时序图</span>
      <div className="conversation-mermaid-header-actions">
        {mode === 'image' && !!svg && <button type="button" className="conversation-mermaid-fullscreen-button" onClick={openFullscreen}>全屏放大</button>}
        <div role="group" aria-label="图表显示方式">
        <button type="button" className={mode === 'image' ? 'selected' : ''} aria-pressed={mode === 'image'} onClick={() => setMode('image')}>图片</button>
        <button type="button" className={mode === 'text' ? 'selected' : ''} aria-pressed={mode === 'text'} onClick={() => setMode('text')}>文本</button>
        </div>
      </div>
    </header>
    {mode === 'image'
      ? <div className="conversation-mermaid-image" aria-busy={rendering}>
          {rendering && <span className="conversation-mermaid-status" role="status">正在生成图片…</span>}
          {!rendering && svg && <div className="conversation-mermaid-svg" role="img" aria-label="Mermaid 图表" dangerouslySetInnerHTML={{ __html: svg }}/>}
          {!rendering && error && <p className="conversation-mermaid-error" role="status">图片生成失败：{error}。可切换到文本查看或复制原始内容。</p>}
        </div>
      : <div className="conversation-mermaid-text">
          <pre><code>{source}</code></pre>
          <button type="button" className="conversation-mermaid-copy" onClick={copySource}>{copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，请手动复制' : '一键复制'}</button>
        </div>}
    {fullscreenPreview}
  </section>;
}

function MarkdownPre({ children, node: _node, ...props }: ComponentPropsWithoutRef<'pre'> & { node?: unknown }) {
  void _node;
  if (isValidElement(children) && children.type === 'code') {
    const code = children.props as { className?: string; children?: ReactNode };
    const source = codeText(code.children).replace(/\n$/, '');
    if (source && isMermaidDiagram(code.className, source)) return <MermaidDiagram source={source}/>;
  }
  return <pre {...props}>{children}</pre>;
}

export function ConversationMarkdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink, img: MarkdownImage, pre: MarkdownPre }}>{children}</ReactMarkdown>;
}
