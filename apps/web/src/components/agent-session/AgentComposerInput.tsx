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

function isImeComposition(event: ReactKeyboardEvent<HTMLElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

const ATTACHMENT_ALIAS_PATTERN = /(@附件\d+)/g;

function composerNodeText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
  if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return '';
  const element = node as Element;
  const alias = element.getAttribute?.('data-composer-attachment-alias');
  if (alias) return alias;
  if (element.tagName === 'BR') return '\n';
  return Array.from(node.childNodes).map(composerNodeText).join('') + (element.tagName === 'DIV' ? '\n' : '');
}

function selectionOffset(root: HTMLElement, container: Node, offset: number): number {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.setEnd(container, offset);
  return composerNodeText(range.cloneContents()).length;
}

function composerSelection(root: HTMLElement): { start: number; end: number } | undefined {
  const selection = window.getSelection();
  if (!selection?.rangeCount) return undefined;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return undefined;
  return {
    start: selectionOffset(root, range.startContainer, range.startOffset),
    end: selectionOffset(root, range.endContainer, range.endOffset),
  };
}

function placeComposerCaret(root: HTMLElement, offset: number) {
  const range = document.createRange();
  let remaining = offset;
  for (const node of root.childNodes) {
    const length = composerNodeText(node).length;
    if (remaining > length) {
      remaining -= length;
      continue;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      range.setStart(node, Math.min(remaining, node.textContent?.length ?? 0));
    } else if (remaining === 0) {
      range.setStartBefore(node);
    } else {
      range.setStartAfter(node);
    }
    range.collapse(true);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    return;
  }
  range.selectNodeContents(root);
  range.collapse(false);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

function attachmentAliasNodes(draft: string, onRemove: (start: number, length: number) => void) {
  const parts = draft.split(ATTACHMENT_ALIAS_PATTERN);
  let offset = 0;
  return parts.map((part, index) => {
    const start = offset;
    offset += part.length;
    return /^@附件\d+$/.test(part)
      ? <span key={`${start}:${part}`} className="agent-composer-attachment-alias" data-composer-attachment-alias={part} contentEditable={false} aria-label={`附件别名 ${part}`}><span>{part}</span><button type="button" tabIndex={-1} aria-label={`移除 ${part}`} onMouseDown={event => event.preventDefault()} onClick={() => onRemove(start, part.length)}>×</button></span>
      : <span key={index}>{part}</span>;
  });
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
  onPaste: (event: ReactClipboardEvent<HTMLElement>) => void;
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
  const input = useRef<HTMLDivElement>(null);
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
  const updateDraftFromInput = useCallback(() => {
    const value = input.current ? composerNodeText(input.current).replace(/\n$/, '') : draftRef.current;
    updateDraft(value);
  }, [updateDraft]);
  const updateDraftWithCaret = useCallback((value: string, caret: number) => {
    updateDraft(value);
    requestAnimationFrame(() => {
      input.current?.focus();
      if (input.current) placeComposerCaret(input.current, caret);
    });
  }, [updateDraft]);
  const insertDraft = useCallback((value: string) => {
    const current = draftRef.current;
    const selection = input.current ? composerSelection(input.current) : undefined;
    const start = selection?.start ?? current.length;
    const end = selection?.end ?? start;
    updateDraftWithCaret(`${current.slice(0, start)}${value}${current.slice(end)}`, start + value.length);
  }, [updateDraftWithCaret]);
  const removeAttachmentAlias = useCallback((start: number, length: number) => {
    const current = draftRef.current;
    updateDraftWithCaret(`${current.slice(0, start)}${current.slice(start + length)}`, start);
  }, [updateDraftWithCaret]);
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
    const editor = input.current;
    if (!editor || editor.getBoundingClientRect().width <= 0) return;
    const styles = window.getComputedStyle(editor);
    const lineHeight = Number.parseFloat(styles.lineHeight);
    const verticalPadding = Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
    const minHeight = Math.max(Number.parseFloat(styles.minHeight), lineHeight + verticalPadding);
    const maxHeight = lineHeight * 10 + verticalPadding;
    editor.style.height = 'auto';
    const contentHeight = editor.scrollHeight;
    const height = Math.min(Math.max(contentHeight, minHeight), maxHeight);
    editor.style.height = `${height}px`;
    editor.style.setProperty('overflow-y', contentHeight > maxHeight + 1 ? 'auto' : 'hidden', 'important');
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
    <div ref={input} className="agent-composer-editor" data-overflowing={inputOverflowing || undefined} data-placeholder={placeholder} aria-label={ariaLabel} aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} aria-multiline="true" contentEditable={!disabled} role="textbox" suppressContentEditableWarning onInput={updateDraftFromInput} onPaste={onPaste} onKeyDown={event => {
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
    }}>{attachmentAliasNodes(draft, removeAttachmentAlias)}</div>
    {fileDragActive && <div className="agent-composer-file-drop" aria-live="polite">松开以添加附件</div>}
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : trigger!.sigil === '@' ? '选择引用类型' : hasNativeSuggestions ? '选择 OpenHands 原生能力、命令或 MCP' : '选择命令或 MCP'}>{hasSuggestions ? <>{visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '@' && index === 0 && <div className="agent-composer-capability-section">引用类型</div>}{trigger!.sigil === '/' && item.kind === 'NATIVE' && (index === 0 || visible[index - 1]?.kind !== 'NATIVE') && <div className="agent-composer-capability-section">OpenHands 原生能力</div>}{trigger!.sigil === '/' && item.kind !== 'NATIVE' && (index === 0 || visible[index - 1]?.kind === 'NATIVE') && <div className="agent-composer-capability-section">MCP 与命令</div>}<button type="button" role="option" aria-selected={index === activeIndex} aria-disabled={item.available === false || undefined} disabled={item.available === false} className={`${index === activeIndex ? 'active' : ''}${item.available === false ? ' unavailable' : ''}`} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'NATIVE' ? '原生' : item.kind === 'REFERENCE' ? '引用' : 'MCP'}</em></button></div>)}</> : <div className="agent-composer-capability-empty"><span><b>{trigger!.sigil === '@' ? '当前没有匹配的引用类型' : !suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{trigger!.sigil === '@' ? '调整输入关键词以筛选引用类型。' : !suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或管理当前会话能力。'}</small></span></div>}{showCapabilityManager && <div className="agent-composer-capability-manage"><span>管理当前会话能力</span><button type="button" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}>管理</button></div>}</div>}
  </div>;
});
