import type { OpenHandsConversationEvent } from '../../types';

export type FileChangeLineKind = 'context' | 'addition' | 'deletion';

export interface FileChangeLine {
  kind: FileChangeLineKind;
  text: string;
  oldLine?: number;
  newLine?: number;
}

export interface WorkspaceFileChange {
  id: string;
  path: string;
  before: string;
  after: string;
  additions: number;
  deletions: number;
  lines: FileChangeLine[];
}

type PendingPatch = { id: string; path: string; lines: FileChangeLine[] };

function detailString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function editableFileOperation(event: OpenHandsConversationEvent): boolean {
  if (event.event_type !== 'TOOL_RESULT' || event.payload.event_name !== 'FileEditorObservation') return false;
  const details = event.payload.details ?? {};
  if (details.is_error === true) return false;
  return ['create', 'str_replace', 'insert', 'undo_edit'].includes(String(details.command ?? '').toLowerCase());
}

function appliedPatchOperation(event: OpenHandsConversationEvent): boolean {
  if (event.event_type !== 'TOOL_RESULT' || event.payload.event_name !== 'ApplyPatchObservation') return false;
  return event.payload.details?.is_error !== true;
}

function splitLines(value: string): string[] {
  return value === '' ? [] : value.replace(/\r\n/g, '\n').split('\n');
}

function simpleDiff(before: string[], after: string[]): FileChangeLine[] {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix += 1;
  const lines: FileChangeLine[] = before.slice(0, prefix).map((text, index) => ({ kind: 'context', text, oldLine: index + 1, newLine: index + 1 }));
  before.slice(prefix, before.length - suffix).forEach((text, index) => lines.push({ kind: 'deletion', text, oldLine: prefix + index + 1 }));
  after.slice(prefix, after.length - suffix).forEach((text, index) => lines.push({ kind: 'addition', text, newLine: prefix + index + 1 }));
  before.slice(before.length - suffix).forEach((text, index) => lines.push({ kind: 'context', text, oldLine: before.length - suffix + index + 1, newLine: after.length - suffix + index + 1 }));
  return lines;
}

export function fileChangeLines(beforeText: string, afterText: string): FileChangeLine[] {
  const before = splitLines(beforeText);
  const after = splitLines(afterText);
  // A bounded LCS keeps normal edits accurate without turning a large generated
  // file into an expensive browser operation. Large files retain a truthful
  // prefix/suffix diff instead of blocking the workspace.
  if (before.length * after.length > 160_000) return simpleDiff(before, after);
  const width = after.length + 1;
  const table = new Uint16Array((before.length + 1) * width);
  for (let left = before.length - 1; left >= 0; left -= 1) {
    for (let right = after.length - 1; right >= 0; right -= 1) {
      table[left * width + right] = before[left] === after[right]
        ? table[(left + 1) * width + right + 1] + 1
        : Math.max(table[(left + 1) * width + right], table[left * width + right + 1]);
    }
  }
  const lines: FileChangeLine[] = [];
  let left = 0;
  let right = 0;
  while (left < before.length || right < after.length) {
    if (left < before.length && right < after.length && before[left] === after[right]) {
      lines.push({ kind: 'context', text: before[left], oldLine: left + 1, newLine: right + 1 });
      left += 1; right += 1;
    } else if (right < after.length && (left === before.length || table[left * width + right + 1] >= table[(left + 1) * width + right])) {
      lines.push({ kind: 'addition', text: after[right], newLine: right + 1 });
      right += 1;
    } else {
      lines.push({ kind: 'deletion', text: before[left], oldLine: left + 1 });
      left += 1;
    }
  }
  return lines;
}

function patchChanges(event: OpenHandsConversationEvent): PendingPatch[] {
  if (event.event_type !== 'TOOL_CALL' || event.payload.event_name !== 'ApplyPatchAction') return [];
  const patch = detailString(event.payload.details?.patch);
  if (!patch) return [];
  const changes: PendingPatch[] = [];
  let current: PendingPatch | undefined;
  for (const rawLine of patch.replace(/\r\n/g, '\n').split('\n')) {
    const match = /^\*\*\* (?:Update|Add|Delete) File: (.+)$/.exec(rawLine);
    if (match) {
      current = { id: `${event.id}:${match[1]}`, path: match[1], lines: [] };
      changes.push(current);
      continue;
    }
    if (!current || rawLine.startsWith('***') || rawLine.startsWith('@@')) continue;
    if (rawLine.startsWith('+')) current.lines.push({ kind: 'addition', text: rawLine.slice(1) });
    else if (rawLine.startsWith('-')) current.lines.push({ kind: 'deletion', text: rawLine.slice(1) });
    else if (rawLine.startsWith(' ')) current.lines.push({ kind: 'context', text: rawLine.slice(1) });
  }
  return changes.filter(change => change.lines.some(line => line.kind !== 'context'));
}

function numberPatchLines(lines: FileChangeLine[]): FileChangeLine[] {
  let oldLine = 1;
  let newLine = 1;
  return lines.map(line => {
    if (line.kind === 'addition') return { ...line, newLine: newLine++ };
    if (line.kind === 'deletion') return { ...line, oldLine: oldLine++ };
    return { ...line, oldLine: oldLine++, newLine: newLine++ };
  });
}

export function workspaceFileChanges(events: OpenHandsConversationEvent[]): WorkspaceFileChange[] {
  const changes = new Map<string, { id: string; path: string; before: string; after: string }>();
  const patchActions = new Map<string, PendingPatch[]>();
  const patchChangesByPath = new Map<string, WorkspaceFileChange>();
  for (const event of events) {
    const patches = patchChanges(event);
    if (patches.length) patchActions.set(event.id, patches);
    if (appliedPatchOperation(event)) {
      const actionId = detailString(event.payload.action_id);
      for (const patchChange of actionId ? patchActions.get(actionId) ?? [] : []) {
        const lines = numberPatchLines(patchChange.lines);
        const existing = patchChangesByPath.get(patchChange.path);
        const combined = existing ? [...existing.lines, ...lines] : lines;
        patchChangesByPath.set(patchChange.path, {
          id: existing?.id ?? patchChange.id, path: patchChange.path,
          before: combined.filter(line => line.kind !== 'addition').map(line => line.text).join('\n'),
          after: combined.filter(line => line.kind !== 'deletion').map(line => line.text).join('\n'),
          additions: combined.filter(line => line.kind === 'addition').length,
          deletions: combined.filter(line => line.kind === 'deletion').length,
          lines: combined,
        });
      }
    }
    if (!editableFileOperation(event)) continue;
    const details = event.payload.details ?? {};
    const path = detailString(details.path);
    const after = detailString(details.new_content);
    if (!path || after === undefined) continue;
    const before = detailString(details.old_content) ?? '';
    const existing = changes.get(path);
    changes.set(path, existing ? { ...existing, after } : { id: event.id, path, before, after });
  }
  const fileEditorChanges = [...changes.values()].map(change => {
    const lines = fileChangeLines(change.before, change.after);
    return {
      ...change,
      additions: lines.filter(line => line.kind === 'addition').length,
      deletions: lines.filter(line => line.kind === 'deletion').length,
      lines,
    };
  });
  // FileEditor observations contain a whole-file before/after snapshot, so
  // they take precedence when both native tools touch one path in a turn.
  for (const change of fileEditorChanges) patchChangesByPath.set(change.path, change);
  return [...patchChangesByPath.values()];
}
