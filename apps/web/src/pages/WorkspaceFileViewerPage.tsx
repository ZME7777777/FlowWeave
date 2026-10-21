import { useQuery } from '@tanstack/react-query';
import { AlertCircle, Download, FileWarning, LoaderCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import { api, agentWorkspaceFileUrl } from '../api/client';
import { sandboxedWorkspaceHtmlDocument } from '../components/workspace-file-preview';
import type { WorkspaceFileViewerRoute } from './workspace-file-viewer-route';

function isHtml(path: string) {
  return /\.html?$/i.test(path);
}

function isImage(path: string) {
  return /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(path);
}

function isPdf(path: string) {
  return /\.pdf$/i.test(path);
}

export function WorkspaceFileViewerPage({ route }: { route: WorkspaceFileViewerRoute }) {
  const options = {
    bindingId: route.bindingId,
    workDirectoryId: route.workDirectoryId,
    draftAttachmentOwnerId: route.draftAttachmentOwnerId,
  };
  const preview = useQuery({
    queryKey: ['workspace-file-viewer', route.workspaceId, route.path, route.bindingId, route.workDirectoryId, route.draftAttachmentOwnerId],
    queryFn: async ({ signal }) => {
      let result = await api.agentWorkspaceFilePreview(route.workspaceId, route.path, options, 0, signal);
      while (isHtml(route.path) && result.nextOffset !== undefined) {
        const next = await api.agentWorkspaceFilePreview(route.workspaceId, route.path, options, result.nextOffset, signal);
        result = { content: result.content + next.content, totalBytes: next.totalBytes || result.totalBytes, nextOffset: next.nextOffset };
      }
      return result;
    },
    retry: false,
    enabled: isHtml(route.path) || !isImage(route.path) && !isPdf(route.path),
  });
  const fileUrl = agentWorkspaceFileUrl(route.workspaceId, route.path, { ...options, download: false });
  const downloadUrl = agentWorkspaceFileUrl(route.workspaceId, route.path, { ...options, download: true });
  const filename = route.path.split('/').filter(Boolean).at(-1) ?? '工作区文件';

  let content: ReactNode;
  if (isImage(route.path)) content = <img className="workspace-file-viewer-media" src={fileUrl} alt={filename}/>;
  else if (isPdf(route.path)) content = <iframe className="workspace-file-viewer-media" title={filename} src={fileUrl}/>;
  else if (preview.isLoading) content = <p className="workspace-file-viewer-state"><LoaderCircle className="spin" size={22}/>正在加载文件…</p>;
  else if (preview.isError) content = <p className="workspace-file-viewer-state error"><AlertCircle size={20}/>文件不可访问：请确认仍已登录且拥有当前会话或工作目录的访问权限。</p>;
  else if (isHtml(route.path) && preview.data) content = <iframe className="workspace-file-viewer-html" sandbox="" title={filename} srcDoc={sandboxedWorkspaceHtmlDocument(preview.data.content, route.path, path => agentWorkspaceFileUrl(route.workspaceId, path, { ...options, download: false }))}/>;
  else if (preview.data) content = <pre className="workspace-file-viewer-text">{preview.data.content}</pre>;
  else content = <p className="workspace-file-viewer-state"><FileWarning size={20}/>此文件不提供在线预览，请下载后查看。</p>;

  return <main className="workspace-file-viewer-page">
    <header><div><span>WORKSPACE FILE</span><h1>{filename}</h1><p>{route.path}</p></div><a href={downloadUrl}><Download size={16}/>下载</a></header>
    <section aria-label={`工作区文件预览：${filename}`}>{content}</section>
  </main>;
}
