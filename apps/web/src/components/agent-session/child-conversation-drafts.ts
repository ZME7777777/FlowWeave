import type {
  AgentAttachment,
  AgentConversationAnnotation,
  AgentConversationReference,
  AgentWorkspaceReference,
} from '../../types';

export interface ChildMessageDraft {
  content: string;
  attachments: AgentAttachment[];
  references: AgentConversationReference[];
  workspaceReferences: AgentWorkspaceReference[];
  annotations: AgentConversationAnnotation[];
}

export interface ChildQueuedMessage extends ChildMessageDraft {
  id: string;
  state: 'queued' | 'sending' | 'failed';
  error?: string;
}

export interface ChildComposerDraft extends ChildMessageDraft {
  scope: string;
  queue: ChildQueuedMessage[];
  autoSend: boolean;
  admissionEventId?: string;
  admittedAt?: number;
}

export function emptyChildMessage(): ChildMessageDraft {
  return { content: '', attachments: [], references: [], workspaceReferences: [], annotations: [] };
}

function message(value: unknown): ChildMessageDraft | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const item = value as Partial<ChildMessageDraft>;
  if (
    typeof item.content !== 'string' ||
    !Array.isArray(item.attachments) ||
    !Array.isArray(item.references) ||
    !Array.isArray(item.workspaceReferences) ||
    !Array.isArray(item.annotations)
  )
    return undefined;
  return {
    content: item.content.slice(0, 200_000),
    attachments: item.attachments
      .filter(
        (attachment) => attachment && typeof attachment.path === 'string' && typeof attachment.filename === 'string',
      )
      .slice(0, 10),
    references: item.references
      .filter(
        (reference) =>
          reference &&
          typeof reference.event_id === 'string' &&
          typeof reference.source_binding_id === 'string' &&
          typeof reference.content === 'string',
      )
      .slice(0, 10),
    workspaceReferences: item.workspaceReferences
      .filter(
        (reference) =>
          reference &&
          typeof reference.path === 'string' &&
          typeof reference.display_name === 'string' &&
          ['file', 'directory'].includes(reference.kind),
      )
      .slice(0, 20),
    annotations: item.annotations
      .filter(
        (annotation) =>
          annotation &&
          typeof annotation.id === 'string' &&
          annotation.anchor &&
          typeof annotation.comment === 'string',
      )
      .slice(0, 20),
  };
}

function key(scope: string) {
  return `flowweave.child-composer.v1:${scope}`;
}

export function readChildComposer(scope: string): ChildComposerDraft {
  const empty = { ...emptyChildMessage(), scope, queue: [], autoSend: true };
  try {
    const raw = sessionStorage.getItem(key(scope));
    if (!raw) return empty;
    const value = JSON.parse(raw) as Partial<ChildComposerDraft>;
    const draft = message(value);
    if (!draft || value.scope !== scope) return empty;
    const queue = Array.isArray(value.queue)
      ? value.queue
          .flatMap((item) => {
            const payload = message(item);
            if (!payload || typeof item.id !== 'string' || !/^[0-9a-f-]{36}$/i.test(item.id)) return [];
            // A browser reload never retries an indeterminate send automatically.
            const state = item.state === 'queued' ? ('queued' as const) : ('failed' as const);
            return [
              {
                ...payload,
                id: item.id,
                state,
                error: state === 'failed' ? '请确认本条消息的发送结果，再使用原提交身份重试。' : undefined,
              },
            ];
          })
          .slice(0, 30)
      : [];
    return {
      ...draft,
      scope,
      queue,
      autoSend: value.autoSend !== false,
      admissionEventId: typeof value.admissionEventId === 'string' ? value.admissionEventId : undefined,
      admittedAt: typeof value.admittedAt === 'number' ? value.admittedAt : undefined,
    };
  } catch {
    return empty;
  }
}

export function writeChildComposer(value: ChildComposerDraft) {
  try {
    sessionStorage.setItem(key(value.scope), JSON.stringify(value));
  } catch {
    /* The mounted pane retains the same complete scoped snapshot in memory. */
  }
}
