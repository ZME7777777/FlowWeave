import { expect, test, type Route } from '@playwright/test';

const workspaceId = 'viewer-workspace';
const bindingId = 'viewer-conversation';
const filePath = '/runtime/workspace/project/reports/daily.html';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('same-origin workspace viewer link renders an authorized HTML file safely', async ({ page }) => {
  let browserProxyRequests = 0;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/me')) return json(route, { id: 'viewer-user', username: 'viewer-user', role: 'USER', can_use_agent_sessions: true });
    if (path.includes('/browser/')) {
      browserProxyRequests += 1;
      return json(route, { error: { code: 'UNEXPECTED_BROWSER_PROXY', message: '不应进入 Runtime 浏览器代理' } }, 500);
    }
    if (path === `/api/v1/agent-workspaces/${workspaceId}/workspace/file`) {
      if (url.searchParams.get('path') === filePath) {
        expect(url.searchParams.get('binding_id')).toBe(bindingId);
        expect(url.searchParams.get('preview')).toBe('true');
        return route.fulfill({
          status: 200,
          contentType: 'text/html',
          headers: { 'X-Preview-Total-Bytes': '206' },
          body: '<!doctype html><html><head><style>.report{color:rgb(12,94,56)}</style></head><body><h1 class="report">受控日报</h1><img src="chart.svg" alt="日报图表"><script>document.body.dataset.scriptRan="true"</script><form><button>不应显示</button></form></body></html>',
        });
      }
      if (url.searchParams.get('path') === '/runtime/workspace/project/reports/chart.svg') {
        return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#0c5e38"/></svg>' });
      }
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: `unexpected ${path}` } }, 404);
  });

  await page.goto(`/workspace-files/view?workspace_id=${workspaceId}&binding_id=${bindingId}&path=${encodeURIComponent(filePath)}`);
  await expect(page.getByRole('heading', { name: 'daily.html' })).toBeVisible();
  const htmlFrame = page.getByTitle('daily.html').contentFrame();
  await expect(htmlFrame.getByRole('heading', { name: '受控日报' })).toBeVisible();
  await expect(htmlFrame.getByRole('button', { name: '不应显示' })).toHaveCount(0);
  await expect(htmlFrame.locator('body')).not.toHaveAttribute('data-script-ran', 'true');
  expect(browserProxyRequests).toBe(0);
});
