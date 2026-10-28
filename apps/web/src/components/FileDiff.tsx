import { filePreviewLanguage, highlightedCode, splitDiffRows, diffChangeStartIndexes, gitDiffLines, type GitDiffLine, type SplitDiffRow } from './fileDiffModel';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type WheelEvent as ReactWheelEvent } from 'react';
import '../pages/agent-workbench.css';

/**
 * Diff payloads contain only source lines (their Git prefix and hunk headers
 * have already been parsed away), so they can use the same safe highlighter as
 * a workspace source preview.  Keep the path at the call site: a Git Diff is
 * repository-relative, while a review Diff is workspace-relative.
 */
export function HighlightedDiffCode({ path, value }: { path: string; value: string }) {
  return <code dangerouslySetInnerHTML={{ __html: highlightedCode(value, filePreviewLanguage(path)) }}/>;
}


/**
 * A split diff has one horizontal code position, not two independently
 * scrollable panes. The bottom scrollbar drives the clipped code bodies in
 * both columns while their headers, gutters, and divider stay put.
 */
export function SharedSplitDiff({ before, after, resetKey }: { before: ReactNode; after: ReactNode; resetKey: string }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scrollbarRef = useRef<HTMLDivElement>(null);
  const [scrollbarWidth, setScrollbarWidth] = useState(0);
  const applyOffset = useCallback((scrollLeft: number) => {
    viewportRef.current?.querySelectorAll<HTMLElement>('.agent-diff-split-code-content').forEach(content => {
      content.style.setProperty('--agent-diff-code-offset', `-${scrollLeft}px`);
    });
  }, []);
  const measureScrollbar = useCallback(() => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    const lines = Array.from(viewport.querySelectorAll<HTMLElement>('.agent-diff-split .agent-diff-line:not(.empty)'));
    // The gutter is deliberately fixed while only the code cell moves.  The
    // scroll range must therefore be calculated from each code cell's actual
    // visible width, not from the entire half-pane; otherwise it reaches its
    // end one gutter-width before a long line is fully revealed.
    const maxOffset = Math.max(0, ...lines.map(line => {
      const code = line.querySelector<HTMLElement>('code');
      if (!code) return 0;
      const visibleCodeWidth = Math.max(0, line.clientWidth - code.offsetLeft);
      return Math.max(0, code.scrollWidth - visibleCodeWidth);
    }));
    setScrollbarWidth(Math.ceil(scrollbar.clientWidth + maxOffset));
  }, []);
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    viewport.scrollTop = 0;
    scrollbar.scrollLeft = 0;
    applyOffset(0);
    measureScrollbar();
    const observer = new ResizeObserver(measureScrollbar);
    observer.observe(viewport);
    observer.observe(scrollbar);
    return () => observer.disconnect();
  }, [applyOffset, measureScrollbar, resetKey]);
  const scrollSplitCode = (event: ReactWheelEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    const horizontalDelta = event.deltaX || (event.shiftKey ? event.deltaY : 0);
    if (horizontalDelta) {
      event.preventDefault();
      event.stopPropagation();
      const maxLeft = Math.max(0, scrollbar.scrollWidth - scrollbar.clientWidth);
      scrollbar.scrollLeft = Math.min(maxLeft, Math.max(0, scrollbar.scrollLeft + horizontalDelta));
      applyOffset(scrollbar.scrollLeft);
      return;
    }
    const maxTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const reachesVerticalBoundary = (event.deltaY < 0 && viewport.scrollTop <= 0)
      || (event.deltaY > 0 && viewport.scrollTop >= maxTop - 1);
    event.stopPropagation();
    if (reachesVerticalBoundary) {
      event.preventDefault();
      viewport.scrollTop = Math.min(maxTop, Math.max(0, viewport.scrollTop + event.deltaY));
    }
  };
  return <div className="agent-diff-split">
    <div ref={viewportRef} className="agent-diff-split-viewport" onWheelCapture={scrollSplitCode}>
      <div className="agent-diff-split-content">
        <div className="agent-diff-before agent-diff-split-pane"><header>修改前</header><div className="agent-diff-split-code-content">{before}</div></div>
        <div className="agent-diff-split-pane"><header>修改后</header><div className="agent-diff-split-code-content">{after}</div></div>
      </div>
      <div className="agent-diff-scroll-end-spacer" aria-hidden="true"/>
    </div>
    <div ref={scrollbarRef} className="agent-diff-split-scrollbar" aria-label="同步查看两侧被截断的代码" onScroll={event => applyOffset(event.currentTarget.scrollLeft)}><div style={{ width: scrollbarWidth }}/></div>
  </div>;
}

export function DiffChangeNavigation({ rootRef, resetKey, changeIndexes }: { rootRef: React.RefObject<HTMLElement | null>; resetKey: string; changeIndexes: number[] }) {
  const activeChange = useRef(-1);
  useEffect(() => { activeChange.current = -1; }, [resetKey]);
  const move = (direction: -1 | 1) => {
    if (!changeIndexes.length) return;
    activeChange.current = (activeChange.current + direction + changeIndexes.length) % changeIndexes.length;
    const target = rootRef.current?.querySelector<HTMLElement>(`[data-diff-change-index="${changeIndexes[activeChange.current]}"]`);
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
  return <div className="agent-diff-change-navigation" aria-label="改动位置导航">
    <button type="button" aria-label="上一处改动" title="上一处改动" disabled={!changeIndexes.length} onClick={() => move(-1)}><ArrowUp size={14}/></button>
    <button type="button" aria-label="下一处改动" title="下一处改动" disabled={!changeIndexes.length} onClick={() => move(1)}><ArrowDown size={14}/></button>
  </div>;
}


/** Native unified patches share the same renderer in Git and capability reviews. */
export function FileDiff({ path, patch, unavailable = false }: { path: string; patch: string; unavailable?: boolean }) {
  const [mode, setMode] = useState<'unified' | 'split'>('split');
  const rootRef = useRef<HTMLElement>(null);
  const lines = useMemo(() => gitDiffLines(patch), [patch]);
  const rows = useMemo(() => splitDiffRows(lines, (previous, next) => previous.hunk !== next.hunk), [lines]);
  const changes = diffChangeStartIndexes(lines);
  const resetKey = `${path}:${patch}`;
  const render = (entry: SplitDiffRow<GitDiffLine>['before'], side: 'before' | 'after', index: number) => {
    if (!entry) return <div key={index} className="agent-diff-line empty" aria-hidden="true"/>;
    return <div key={index} className={`agent-diff-line ${entry.line.kind}`} data-diff-change-index={changes.includes(entry.index) ? entry.index : undefined}><i>{(side === 'before' ? entry.line.oldLine : entry.line.newLine) ?? ''}</i><HighlightedDiffCode path={path} value={entry.line.text || ' '}/></div>;
  };
  return <section className="agent-git-file-diff-review capability-file-diff" ref={rootRef}>
    <header><span>上一版本 → 当前版本</span><div className="agent-changes-diff-actions"><DiffChangeNavigation rootRef={rootRef} resetKey={resetKey} changeIndexes={changes}/><div className="agent-diff-mode"><button type="button" className={mode === 'unified' ? 'active' : ''} onClick={() => setMode('unified')}>统一</button><button type="button" className={mode === 'split' ? 'active' : ''} onClick={() => setMode('split')}>并排</button></div></div></header>
    {unavailable ? <div className="capability-package-empty">二进制或大文件不支持行级对比，可下载两版查看。</div> : !lines.length ? <div className="capability-package-empty">文件内容相同</div> : mode === 'unified'
      ? <pre className="agent-diff-unified" key={resetKey}>{lines.map((line, index) => <div key={index} className={`agent-diff-line ${line.kind}`} data-diff-change-index={changes.includes(index) ? index : undefined}><i>{line.oldLine ?? line.newLine ?? ''}</i><strong>{line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '-' : ' '}</strong><HighlightedDiffCode path={path} value={line.text || ' '}/></div>)}<div className="agent-diff-scroll-end-spacer" aria-hidden="true"/></pre>
      : <SharedSplitDiff resetKey={resetKey} before={rows.map((row, index) => render(row.before, 'before', index))} after={rows.map((row, index) => render(row.after, 'after', index))}/>}
  </section>;
}
