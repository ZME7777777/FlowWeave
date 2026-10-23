export function sandboxedWorkspaceHtmlDocument(content: string, sourcePath: string, workspaceFileUrl: (path: string) => string): string {
  const document = new DOMParser().parseFromString(content, 'text/html');
  const resourceUrl = (value: string): string | undefined => {
    const source = value.trim();
    if (!source || source.startsWith('#') || /^(?:data|blob):/i.test(source)) return source;
    if (/^[a-z][a-z\d+.-]*:/i.test(source) || source.startsWith('//')) return undefined;
    try {
      const sourceDirectory = sourcePath.slice(0, sourcePath.lastIndexOf('/') + 1);
      const resolved = new URL(source, `https://workspace.invalid${sourceDirectory}`).pathname;
      return workspaceFileUrl(decodeURIComponent(resolved));
    } catch {
      return undefined;
    }
  };
  const rewriteCssUrls = (css: string) => css.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/gi, (match, _quote, value: string) => {
    const resolved = resourceUrl(value);
    return resolved ? `url("${resolved}")` : match;
  });
  const rewriteAttribute = (element: Element, name: string) => {
    const value = element.getAttribute(name);
    if (value === null) return;
    const resolved = resourceUrl(value);
    if (resolved === undefined) element.removeAttribute(name);
    else element.setAttribute(name, resolved);
  };

  document.querySelectorAll('script, base, object, embed, iframe, form, button, input, select, textarea').forEach(element => element.remove());
  document.querySelectorAll('meta[http-equiv]').forEach(element => element.remove());
  document.querySelectorAll<HTMLElement>('*').forEach(element => {
    for (const attribute of [...element.attributes]) {
      if (attribute.name.toLowerCase().startsWith('on')) element.removeAttribute(attribute.name);
    }
    if (element.hasAttribute('style')) element.setAttribute('style', rewriteCssUrls(element.getAttribute('style') ?? ''));
  });
  document.querySelectorAll('img[src], audio[src], video[src], source[src], track[src], input[src], image[href], image[xlink\\:href]').forEach(element => {
    rewriteAttribute(element, element.hasAttribute('src') ? 'src' : element.hasAttribute('href') ? 'href' : 'xlink:href');
  });
  document.querySelectorAll('video[poster]').forEach(element => rewriteAttribute(element, 'poster'));
  document.querySelectorAll('link[rel~="stylesheet"][href]').forEach(element => rewriteAttribute(element, 'href'));
  document.querySelectorAll('style').forEach(element => { element.textContent = rewriteCssUrls(element.textContent ?? ''); });

  // Files remain script-free. Native links can open outside the sandbox without
  // replacing the report or inheriting its restrictions in the new browser tab.
  document.querySelectorAll('a[href], area[href]').forEach(link => {
    const href = link.getAttribute('href')?.trim() ?? '';
    if (!/^(?:https?:)?\/\//i.test(href)) return;
    try {
      const url = new URL(href, window.location.href);
      const localService = ['localhost', '0.0.0.0', '[::1]'].includes(url.hostname) || /^127\.\d+\.\d+\.\d+$/.test(url.hostname);
      if (url.origin === window.location.origin || localService) return;
      link.setAttribute('href', url.href);
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    } catch { /* Ignore invalid links. */ }
  });

  const origin = window.location.origin;
  const csp = document.createElement('meta');
  csp.httpEquiv = 'Content-Security-Policy';
  csp.content = `default-src 'none'; base-uri 'none'; script-src 'none'; connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; style-src 'unsafe-inline' ${origin}; img-src data: blob: ${origin}; font-src data: ${origin}; media-src ${origin}`;
  (document.head || document.documentElement).prepend(csp);
  return `<!doctype html>\n${document.documentElement.outerHTML}`;
}
