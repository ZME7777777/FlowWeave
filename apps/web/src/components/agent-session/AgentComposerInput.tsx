import { ChevronRight, Boxes, Target } from 'lucide-react';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { CONVERSATION_REFERENCE_TRANSFER_TYPE, WORKSPACE_FILE_TRANSFER_TYPE, transferredConversationReference, transferredFiles, transferredWorkspacePaths, type ComposerHandle } from './agentComposerInputUtils';

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

function composerTrigger(value: string, cursor: number): { sigil: '$' | '/' | '@'; query: string; start: number; end: number } | undefined {
  const match = /(?:^| )([$/@])([^\s]*)$/.exec(value.slice(0, cursor));
  if (!match) return undefined;
  const start = match.index + match[0].indexOf(match[1]);
  return { sigil: match[1] as '$' | '/' | '@', query: match[2], start, end: cursor };
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
  onDropConversationReference?: (reference: { bindingId: string; eventId: string; title: string }) => void;
  onSubmit: (content: string) => void;
  onDirectSubmit?: (content: string) => void;
  onManageCapabilities?: () => void;
  onNativeAction?: (action: NativeComposerAction) => void;
  onWorkspaceReferenceSelected?: () => void;
  goalModeEnabled?: boolean;
  goalModeAvailable?: boolean;
  onGoalModeToggle?: () => void;
}>(function AgentComposerInput({
  ariaLabel = '发送 Agent 消息', initialDraft, scope, suggestions, disabled, placeholder, onDraftChange, onContentPresenceChange, onDraftPersist, onPaste, onDropFiles, onDropWorkspaceFiles, onDropConversationReference, onSubmit, onDirectSubmit, onManageCapabilities, onNativeAction, onWorkspaceReferenceSelected, goalModeEnabled = false, goalModeAvailable = false, onGoalModeToggle,
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
  const [selectionStart, setSelectionStart] = useState(() => initialDraft.length);
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
      setSelectionStart(cursor);
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
    setSelectionStart(initialDraft.length);
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
    const horizontalPadding = Number.parseFloat(styles.paddingLeft) + Number.parseFloat(styles.paddingRight);
    const minHeight = Math.max(Number.parseFloat(styles.minHeight), lineHeight + verticalPadding);
    const maxHeight = lineHeight * 10 + verticalPadding;
    const measurement = document.createElement('div');
    // A div drops its trailing empty line; the textarea still needs room for its caret.
    measurement.textContent = `${textarea.value}\u200b`;
    measurement.setAttribute('aria-hidden', 'true');
    measurement.style.setProperty('position', 'fixed', 'important');
    measurement.style.setProperty('visibility', 'hidden', 'important');
    measurement.style.setProperty('pointer-events', 'none', 'important');
    measurement.style.setProperty('box-sizing', 'content-box', 'important');
    measurement.style.setProperty('width', `${Math.max(0, textarea.clientWidth - horizontalPadding)}px`, 'important');
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
    const measuredContentHeight = measurement.getBoundingClientRect().height;
    measurement.remove();
    const contentHeight = measuredContentHeight + verticalPadding;
    const height = Math.min(Math.max(contentHeight, minHeight), maxHeight);
    textarea.style.height = `${height}px`;
    textarea.style.setProperty('overflow-y', contentHeight > maxHeight + 1 ? 'auto' : 'hidden', 'important');
    const overflowing = contentHeight > maxHeight + 1;
    if (!overflowing && textarea.scrollTop !== 0) textarea.scrollTop = 0;
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
  const trigger = composerTrigger(draft, selectionStart);
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
      updateDraft(`${draft.slice(0, trigger.start)}${draft.slice(trigger.end)}`);
      setSelectionStart(trigger.start);
      onNativeAction?.(item.nativeAction);
      return;
    }
    if (item.kind === 'REFERENCE') {
      updateDraft(`${draft.slice(0, trigger.start)}${draft.slice(trigger.end)}`);
      setSelectionStart(trigger.start);
      onWorkspaceReferenceSelected?.();
      return;
    }
    const replacement = `${item.token} `;
    updateDraft(`${draft.slice(0, trigger.start)}${replacement}${draft.slice(trigger.end)}`);
    requestAnimationFrame(() => {
      const cursor = trigger.start + replacement.length;
      setSelectionStart(cursor);
      input.current?.focus();
      input.current?.setSelectionRange(cursor, cursor);
    });
  };
  const hasSuggestions = visible.length > 0;
  const showCapabilityManager = Boolean(trigger && trigger.sigil !== '@' && onManageCapabilities);
  const hasMenu = Boolean(trigger && (hasSuggestions || showCapabilityManager));
  const hasNativeSuggestions = suggestions.some(item => item.kind === 'NATIVE');
  const acceptsFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes('Files');
  const acceptsWorkspaceFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE);
  const acceptsConversationReferenceDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes(CONVERSATION_REFERENCE_TRANSFER_TYPE);
  const acceptsAttachmentDrag = (event: ReactDragEvent<HTMLElement>) => (Boolean(onDropFiles) && acceptsFileDrag(event)) || (Boolean(onDropWorkspaceFiles) && acceptsWorkspaceFileDrag(event)) || (Boolean(onDropConversationReference) && acceptsConversationReferenceDrag(event));
  const [dragKind, setDragKind] = useState<'attachment' | 'conversation'>('attachment');
  const clearFileDrag = () => { dragDepth.current = 0; setFileDragActive(false); setDragKind('attachment'); };
  return <div className={`agent-composer-input${fileDragActive ? ' file-drag-active' : ''}`} onDragEnter={event => {
    if (disabled || !acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragKind(acceptsConversationReferenceDrag(event) ? 'conversation' : 'attachment');
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
    const conversationReference = disabled ? undefined : transferredConversationReference(event.dataTransfer);
    clearFileDrag();
    if (conversationReference) onDropConversationReference?.(conversationReference);
    if (workspacePaths.length) onDropWorkspaceFiles?.(workspacePaths);
    if (files.length) onDropFiles?.(files);
  }}>
    <textarea ref={input} data-overflowing={inputOverflowing || undefined} aria-label={ariaLabel} aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} value={draft} maxLength={200_000} placeholder={placeholder} disabled={disabled} onChange={event => { setSelectionStart(event.target.selectionStart); updateDraft(event.target.value); }} onSelect={event => setSelectionStart(event.currentTarget.selectionStart)} onPaste={onPaste} onKeyDown={event => {
      if (isImeComposition(event)) return;
      if (event.key === 'Enter' && (event.metaKey || event.altKey) && !event.shiftKey) { event.preventDefault(); onDirectSubmit?.(draftRef.current); return; }
      if (hasMenu && event.key === 'Escape') { updateDraft(`${draft.slice(0, trigger!.start)}${draft.slice(trigger!.end)}`); setSelectionStart(trigger!.start); return; }
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
    {goalModeAvailable && <button type="button" className={`agent-composer-goal-toggle${goalModeEnabled ? ' active' : ''}`} aria-label={goalModeEnabled ? '关闭目标模式' : '开启目标模式'} aria-pressed={goalModeEnabled} title={goalModeEnabled ? '目标模式已开启：发送消息将启动目标循环' : '开启目标模式'} disabled={disabled} onClick={onGoalModeToggle}><Target size={14}/></button>}
    {fileDragActive && <div className="agent-composer-file-drop" aria-live="polite">{dragKind === 'conversation' ? '松开以添加会话' : '松开以添加附件'}</div>}
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : trigger!.sigil === '@' ? '选择引用类型' : hasNativeSuggestions ? '选择 OpenHands 原生能力、命令或 MCP' : '选择命令或 MCP'}>{hasSuggestions ? <>{visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '@' && index === 0 && <div className="agent-composer-capability-section">引用类型</div>}{trigger!.sigil === '/' && item.kind === 'NATIVE' && (index === 0 || visible[index - 1]?.kind !== 'NATIVE') && <div className="agent-composer-capability-section">OpenHands 原生能力</div>}{trigger!.sigil === '/' && item.kind !== 'NATIVE' && (index === 0 || visible[index - 1]?.kind === 'NATIVE') && <div className="agent-composer-capability-section">MCP 与命令</div>}<button type="button" role="option" aria-selected={index === activeIndex} aria-disabled={item.available === false || undefined} disabled={item.available === false} className={`${index === activeIndex ? 'active' : ''}${item.available === false ? ' unavailable' : ''}`} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'NATIVE' ? '原生' : item.kind === 'REFERENCE' ? '引用' : 'MCP'}</em></button></div>)}</> : <div className="agent-composer-capability-empty"><span><b>{trigger!.sigil === '@' ? '当前没有匹配的引用类型' : !suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{trigger!.sigil === '@' ? '调整输入关键词以筛选引用类型。' : !suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或前往会话配置。'}</small></span></div>}{showCapabilityManager && <button type="button" className="agent-composer-capability-manage" aria-label="会话配置" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}><Boxes size={16}/><b>会话配置</b><ChevronRight size={16}/></button>}</div>}
  </div>;
});
