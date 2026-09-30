import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { WORKSPACE_FILE_TRANSFER_TYPE, transferredFiles, type ComposerHandle } from './agentComposerInputUtils';

const COMPOSER_DRAFT_PERSIST_DELAY_MS = 400;

export type NativeComposerAction = 'CONDENSE';
export type ComposerSuggestionKind = 'SKILL' | 'COMMAND' | 'MCP' | 'NATIVE' | 'REFERENCE';

export interface ComposerSuggestion {
  id: string;
  kind: ComposerSuggestionKind;
  token: string;
  label: string;
  detail: string;
  available?: boolean;
  nativeAction?: NativeComposerAction;
}

function transferredWorkspacePaths(transfer: DataTransfer): string[] {
  if (!transfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE)) return [];
  return transfer.getData(WORKSPACE_FILE_TRANSFER_TYPE).split('\n').map(path => path.trim()).filter(Boolean);
}

function composerTrigger(value: string): { sigil: '$' | '/' | '@'; query: string; start: number } | undefined {
  const match = /(?:^|\s)([$/@])([^\s]*)$/.exec(value);
  if (!match) return undefined;
  return { sigil: match[1] as '$' | '/' | '@', query: match[2], start: value.length - match[0].length + (match[0].startsWith(' ') ? 1 : 0) };
}

function isImeComposition(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

export const AgentComposerInput = forwardRef<ComposerHandle, {
  ariaLabel?: string;
  initialDraft: string;
  scope?: string;
  suggestions: ComposerSuggestion[];
  disabled: boolean;
  placeholder: string;
  onDraftChange: (scope: string | undefined, value: string) => void;
  onContentPresenceChange: (hasContent: boolean) => void;
  onDraftPersist: (scope: string | undefined) => void;
  onPaste: (event: ReactClipboardEvent<HTMLTextAreaElement>) => void;
  onDropFiles?: (files: File[]) => void;
  onDropWorkspaceFiles?: (paths: string[]) => void;
  onSubmit: (content: string) => void;
  onDirectSubmit?: (content: string) => void;
  onManageCapabilities?: () => void;
  onNativeAction?: (action: NativeComposerAction) => void;
  onWorkspaceReferenceSelected?: () => void;
}>(function AgentComposerInput({
  ariaLabel = '发送 Agent 消息', initialDraft, scope, suggestions, disabled, placeholder, onDraftChange, onContentPresenceChange, onDraftPersist, onPaste, onDropFiles, onDropWorkspaceFiles, onSubmit, onDirectSubmit, onManageCapabilities, onNativeAction, onWorkspaceReferenceSelected,
}, ref) {
  const input = useRef<HTMLTextAreaElement>(null);
  const dragDepth = useRef(0);
  const draftRef = useRef(initialDraft);
  const previousScope = useRef(scope);
  const persistDraftRef = useRef(onDraftPersist);
  const [draft, setDraft] = useState(initialDraft);
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileDragActive, setFileDragActive] = useState(false);
  const [inputOverflowing, setInputOverflowing] = useState(false);
  const replaceDraft = useCallback((value: string) => {
    draftRef.current = value;
    setDraft(current => current === value ? current : value);
  }, []);
  const updateDraft = useCallback((value: string) => {
    replaceDraft(value);
    onDraftChange(scope, value);
  }, [onDraftChange, replaceDraft, scope]);
  const insertDraft = useCallback((value: string) => {
    const textarea = input.current;
    const current = draftRef.current;
    const start = textarea?.selectionStart ?? current.length;
    const end = textarea?.selectionEnd ?? start;
    const next = `${current.slice(0, start)}${value}${current.slice(end)}`;
    updateDraft(next);
    requestAnimationFrame(() => {
      textarea?.focus();
      const cursor = start + value.length;
      textarea?.setSelectionRange(cursor, cursor);
    });
  }, [updateDraft]);
  useImperativeHandle(ref, () => ({
    replace: replaceDraft,
    insert: insertDraft,
    value: () => draftRef.current,
    focus: () => input.current?.focus(),
  }), [insertDraft, replaceDraft]);
  useLayoutEffect(() => {
    if (previousScope.current === scope) return;
    previousScope.current = scope;
    replaceDraft(initialDraft);
  }, [initialDraft, replaceDraft, scope]);
  const hasText = Boolean(draft.trim());
  useEffect(() => { onContentPresenceChange(hasText); }, [hasText, onContentPresenceChange]);
  useEffect(() => { persistDraftRef.current = onDraftPersist; }, [onDraftPersist]);
  useEffect(() => {
    const scopeForPersist = scope;
    const timer = window.setTimeout(() => persistDraftRef.current(scopeForPersist), COMPOSER_DRAFT_PERSIST_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [draft, scope]);
  const resizeInput = useCallback(() => {
    const textarea = input.current;
    if (!textarea || textarea.getBoundingClientRect().width <= 0) return;
    const styles = window.getComputedStyle(textarea);
    const lineHeight = Number.parseFloat(styles.lineHeight);
    const verticalPadding = Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
    const minHeight = Math.max(Number.parseFloat(styles.minHeight), lineHeight + verticalPadding);
    const maxHeight = lineHeight * 10 + verticalPadding;
    const measurement = document.createElement('div');
    measurement.textContent = textarea.value || ' ';
    measurement.setAttribute('aria-hidden', 'true');
    measurement.style.setProperty('position', 'fixed', 'important');
    measurement.style.setProperty('visibility', 'hidden', 'important');
    measurement.style.setProperty('pointer-events', 'none', 'important');
    measurement.style.setProperty('box-sizing', 'content-box', 'important');
    measurement.style.setProperty('width', `${Math.max(0, textarea.clientWidth - verticalPadding)}px`, 'important');
    measurement.style.setProperty('margin', '0', 'important');
    measurement.style.setProperty('padding', '0', 'important');
    measurement.style.setProperty('border', '0', 'important');
    measurement.style.setProperty('font', styles.font, 'important');
    measurement.style.setProperty('font-kerning', styles.fontKerning, 'important');
    measurement.style.setProperty('letter-spacing', styles.letterSpacing, 'important');
    measurement.style.setProperty('line-height', styles.lineHeight, 'important');
    measurement.style.setProperty('white-space', 'pre-wrap', 'important');
    measurement.style.setProperty('overflow-wrap', 'anywhere', 'important');
    measurement.style.setProperty('word-break', 'break-word', 'important');
    document.body.append(measurement);
    const contentHeight = measurement.getBoundingClientRect().height + verticalPadding;
    measurement.remove();
    const height = Math.min(Math.max(contentHeight, minHeight), maxHeight);
    textarea.style.height = `${height}px`;
    textarea.style.setProperty('overflow-y', contentHeight > maxHeight + 1 ? 'auto' : 'hidden', 'important');
    const overflowing = contentHeight > maxHeight + 1;
    setInputOverflowing(current => current === overflowing ? current : overflowing);
  }, []);
  useLayoutEffect(() => { resizeInput(); }, [draft, resizeInput]);
  useEffect(() => {
    window.addEventListener('resize', resizeInput);
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(resizeInput);
    if (input.current?.parentElement) observer?.observe(input.current.parentElement);
    const layoutFrame = requestAnimationFrame(resizeInput);
    return () => {
      window.removeEventListener('resize', resizeInput);
      observer?.disconnect();
      cancelAnimationFrame(layoutFrame);
    };
  }, [resizeInput]);
  const trigger = composerTrigger(draft);
  const visible = (() => {
    if (!trigger) return [];
    const needle = trigger.query.toLocaleLowerCase();
    if (trigger.sigil === '@') {
      const reference: ComposerSuggestion = {
        id: 'reference:workspace-files',
        kind: 'REFERENCE',
        token: '@文件和目录',
        label: '文件和目录',
        detail: '引用当前容器工作区中的文件或目录',
      };
      return !needle || `${reference.token} ${reference.label} ${reference.detail}`.toLocaleLowerCase().includes(needle)
        ? [reference]
        : [];
    }
    return suggestions.filter(item => (trigger.sigil === '$' ? item.kind === 'SKILL' : item.kind !== 'SKILL')
      && (!needle || `${item.token} ${item.label} ${item.detail}`.toLocaleLowerCase().includes(needle)));
  })();
  useEffect(() => setActiveIndex(0), [draft]);
  const select = (item: ComposerSuggestion) => {
    if (!trigger || item.available === false) return;
    if (item.kind === 'NATIVE' && item.nativeAction) {
      updateDraft(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onNativeAction?.(item.nativeAction);
      return;
    }
    if (item.kind === 'REFERENCE') {
      updateDraft(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onWorkspaceReferenceSelected?.();
      return;
    }
    updateDraft(`${draft.slice(0, trigger.start)}${item.token} ${draft.slice(trigger.start + trigger.query.length + 1)}`);
    requestAnimationFrame(() => input.current?.focus());
  };
  const hasSuggestions = visible.length > 0;
  const showCapabilityManager = Boolean(trigger && trigger.sigil !== '@' && onManageCapabilities);
  const hasMenu = Boolean(trigger && (hasSuggestions || showCapabilityManager));
  const hasNativeSuggestions = suggestions.some(item => item.kind === 'NATIVE');
  const acceptsFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes('Files');
  const acceptsWorkspaceFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE);
  const acceptsAttachmentDrag = (event: ReactDragEvent<HTMLElement>) => (Boolean(onDropFiles) && acceptsFileDrag(event)) || (Boolean(onDropWorkspaceFiles) && acceptsWorkspaceFileDrag(event));
  const clearFileDrag = () => { dragDepth.current = 0; setFileDragActive(false); };
  return <div className={`agent-composer-input${fileDragActive ? ' file-drag-active' : ''}`} onDragEnter={event => {
    if (disabled || !acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    dragDepth.current += 1;
    setFileDragActive(true);
  }} onDragOver={event => {
    if (disabled || !acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }} onDragLeave={() => {
    if (!fileDragActive) return;
    dragDepth.current -= 1;
    if (dragDepth.current <= 0) clearFileDrag();
  }} onDrop={event => {
    if (!acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    const workspacePaths = disabled ? [] : transferredWorkspacePaths(event.dataTransfer);
    const files = disabled ? [] : transferredFiles(event.dataTransfer);
    clearFileDrag();
    if (workspacePaths.length) onDropWorkspaceFiles?.(workspacePaths);
    if (files.length) onDropFiles?.(files);
  }}>
    <textarea ref={input} data-overflowing={inputOverflowing || undefined} aria-label={ariaLabel} aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} value={draft} maxLength={200_000} placeholder={placeholder} disabled={disabled} onChange={event => updateDraft(event.target.value)} onPaste={onPaste} onKeyDown={event => {
      if (isImeComposition(event)) return;
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.shiftKey) { event.preventDefault(); onDirectSubmit?.(draftRef.current); return; }
      if (hasMenu && event.key === 'Escape') { updateDraft(draft.slice(0, -trigger!.query.length - 1)); return; }
      if (hasSuggestions && ['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'ArrowDown') { setActiveIndex(index => (index + 1) % visible.length); return; }
        if (event.key === 'ArrowUp') { setActiveIndex(index => (index - 1 + visible.length) % visible.length); return; }
        select(visible[activeIndex] ?? visible[0]);
        return;
      }
      if (hasMenu && ['Enter', 'Tab'].includes(event.key)) { event.preventDefault(); return; }
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); onSubmit(draftRef.current); }
    }}/>
    {fileDragActive && <div className="agent-composer-file-drop" aria-live="polite">松开以添加附件</div>}
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : trigger!.sigil === '@' ? '选择引用类型' : hasNativeSuggestions ? '选择 OpenHands 原生能力、命令或 MCP' : '选择命令或 MCP'}>{hasSuggestions ? <>{visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '@' && index === 0 && <div className="agent-composer-capability-section">引用类型</div>}{trigger!.sigil === '/' && item.kind === 'NATIVE' && (index === 0 || visible[index - 1]?.kind !== 'NATIVE') && <div className="agent-composer-capability-section">OpenHands 原生能力</div>}{trigger!.sigil === '/' && item.kind !== 'NATIVE' && (index === 0 || visible[index - 1]?.kind === 'NATIVE') && <div className="agent-composer-capability-section">MCP 与命令</div>}<button type="button" role="option" aria-selected={index === activeIndex} aria-disabled={item.available === false || undefined} disabled={item.available === false} className={`${index === activeIndex ? 'active' : ''}${item.available === false ? ' unavailable' : ''}`} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'NATIVE' ? '原生' : item.kind === 'REFERENCE' ? '引用' : 'MCP'}</em></button></div>)}</> : <div className="agent-composer-capability-empty"><span><b>{trigger!.sigil === '@' ? '当前没有匹配的引用类型' : !suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{trigger!.sigil === '@' ? '调整输入关键词以筛选引用类型。' : !suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或管理当前会话能力。'}</small></span></div>}{showCapabilityManager && <div className="agent-composer-capability-manage"><span>管理当前会话能力</span><button type="button" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}>管理</button></div>}</div>}
  </div>;
});
