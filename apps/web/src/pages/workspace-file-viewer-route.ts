import { withoutDeploymentBase } from '../deploymentPath';

export interface WorkspaceFileViewerRoute {
  workspaceId: string;
  path: string;
  bindingId?: string;
  workDirectoryId?: string;
  draftAttachmentOwnerId?: string;
}

export function workspaceFileViewerRoute(location: Pick<Location, 'pathname' | 'search'> = window.location): WorkspaceFileViewerRoute | undefined {
  if (withoutDeploymentBase(location.pathname) !== '/workspace-files/view') return undefined;
  const params = new URLSearchParams(location.search);
  const workspaceId = params.get('workspace_id');
  const path = params.get('path');
  if (!workspaceId || !path) return undefined;
  return {
    workspaceId,
    path,
    bindingId: params.get('binding_id') ?? undefined,
    workDirectoryId: params.get('work_directory_id') ?? undefined,
    draftAttachmentOwnerId: params.get('draft_attachment_owner_id') ?? undefined,
  };
}
