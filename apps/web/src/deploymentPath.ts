const viteBase = import.meta.env.BASE_URL || '/';

export const deploymentBasePath = viteBase === '/' ? '' : `/${viteBase.replace(/^\/+|\/+$/g, '')}`;

export function withDeploymentBase(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${deploymentBasePath}${normalized}`;
}

export function withoutDeploymentBase(pathname: string): string {
  if (!deploymentBasePath) return pathname;
  if (pathname === deploymentBasePath) return '/';
  return pathname.startsWith(`${deploymentBasePath}/`)
    ? pathname.slice(deploymentBasePath.length)
    : pathname;
}
