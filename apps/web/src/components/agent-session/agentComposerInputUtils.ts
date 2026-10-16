export const WORKSPACE_FILE_TRANSFER_TYPE = 'application/x-flowweave-workspace-file-path';
export const CONVERSATION_REFERENCE_TRANSFER_TYPE = 'application/x-flowweave-conversation-reference';

export interface ComposerHandle {
  replace: (value: string) => void;
  insert: (value: string) => void;
  value: () => string;
  focus: () => void;
}

export function transferredFiles(transfer: DataTransfer): File[] {
  const files = Array.from(transfer.files);
  if (files.length) return files;
  return Array.from(transfer.items)
    .filter(item => item.kind === 'file')
    .map(item => item.getAsFile())
    .filter((file): file is File => file !== null);
}

export function transferredWorkspacePaths(transfer: DataTransfer): string[] {
  if (!transfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE)) return [];
  return transfer.getData(WORKSPACE_FILE_TRANSFER_TYPE).split('\n').map(path => path.trim()).filter(Boolean);
}

export interface ConversationReferenceTransfer {
  bindingId: string;
  eventId: string;
  title: string;
}

export function transferredConversationReference(transfer: DataTransfer): ConversationReferenceTransfer | undefined {
  if (!transfer.types.includes(CONVERSATION_REFERENCE_TRANSFER_TYPE)) return undefined;
  try {
    const value = JSON.parse(transfer.getData(CONVERSATION_REFERENCE_TRANSFER_TYPE)) as { bindingId?: unknown; eventId?: unknown; title?: unknown };
    return typeof value.bindingId === 'string' && typeof value.eventId === 'string' && typeof value.title === 'string'
      ? { bindingId: value.bindingId, eventId: value.eventId, title: value.title }
      : undefined;
  } catch {
    return undefined;
  }
}
