import { withoutDeploymentBase } from '../../deploymentPath';

/**
 * Resolve a Markdown link only when it stays inside the currently authorized
 * workspace. Relative links in an Agent reply are file references, not web
 * routes; routing them through the drawer avoids a browser-level navigation.
 */
function normalizedWorkspacePath(path: string): string {
  const absolute = path.startsWith('/');
  const parts: string[] = [];
  for (const part of path.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return `${absolute ? '/' : ''}${parts.join('/')}`;
}

export function workspaceMarkdownFileHref(href: string): boolean {
  // Existing Agent replies can contain absolute API links. Route these through
  // the active binding's preview instead of opening an unscoped download URL.
  if (workspaceFileApiPath(href) !== undefined) return true;
  return Boolean(href && !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(href) && href.split(/[?#]/, 1)[0]);
}

export function workspaceFileApiPath(href: string): string | undefined {
  try {
    const url = new URL(href, window.location.origin);
    return url.origin === window.location.origin
      && /^\/api\/v1\/(?:agent-workspaces\/[^/]+|flow-runs\/[^/]+\/(?:node-attempts|attempts)\/[^/]+\/agent-sessions)\/workspace\/file$/.test(withoutDeploymentBase(url.pathname))
      ? url.searchParams.get('path') ?? undefined
      : undefined;
  } catch { return undefined; }
}

export function workspaceMarkdownLinkPath(href: string, workingDirectory?: string, sourcePath?: string): string | undefined {
  const root = workingDirectory ? normalizedWorkspacePath(workingDirectory.replace(/\\/g, '/')) : '';
  if (!root) return undefined;
  try {
    const path = workspaceFileApiPath(href);
    if (path !== undefined) {
      return workspaceMarkdownLinkPath(path, workingDirectory, sourcePath);
    }
  } catch {
    return undefined;
  }
  if (!workspaceMarkdownFileHref(href)) return undefined;
  const rawPath = href.split(/[?#]/, 1)[0];
  if (!rawPath) return undefined;
  let path: string;
  try { path = decodeURIComponent(rawPath).replace(/\\/g, '/'); } catch { return undefined; }
  const normalizedSource = sourcePath ? normalizedWorkspacePath(sourcePath.replace(/\\/g, '/')) : undefined;
  const sourceDirectory = normalizedSource && normalizedSource.startsWith(`${root}/`)
    ? normalizedSource.slice(0, normalizedSource.lastIndexOf('/'))
    : root;
  const resolved = normalizedWorkspacePath(path.startsWith('/') ? path : `${sourceDirectory}/${path}`);
  return resolved === root || resolved.startsWith(`${root}/`) ? resolved : undefined;
}

export function workspaceMarkdownImagePath(src: string, workingDirectory?: string): string | undefined {
  return workspaceMarkdownLinkPath(src.replace(/^sandbox:(?=\/)/i, ''), workingDirectory);
}
