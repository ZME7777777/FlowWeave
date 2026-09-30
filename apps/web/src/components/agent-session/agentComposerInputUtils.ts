export const WORKSPACE_FILE_TRANSFER_TYPE = 'application/x-flowweave-workspace-file-path';

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
