import { expect, test, type Route } from '@playwright/test';

async function respond(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('conversation management keeps the selected workspace and pagination in fixed panes', async ({ page }) => {
  const now = new Date().toISOString();
  const workspace = { id: 'workspace-default', display_name: '默认工作区', desired_state: 'RUNNING', updated_at: now };
  const items = Array.from({ length: 21 }, (_, index) => ({
    id: `conversation-${index + 1}`,
    display_title: `会话 ${index + 1}`,
    pinned: false,
    unread: false,
    created_at: now,
    updated_at: now,
    last_message_at: index === 20 ? new Date(Date.now() - 31 * 24 * 60 * 60 * 1000).toISOString() : now,
  }));

  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) return respond(route, {
      id: 'conversation-manager', username: 'flowweave', role: 'USER', is_super_admin: false,
      can_use_agent_sessions: true,
    });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, workspace);
    if (path.endsWith('/agent-workspaces/workspace-default/work-directories')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: [] });
    if (path.endsWith('/agent-workspaces/workspace-default/conversation-management')) return respond(route, { items, truncated: false });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();

  const layout = page.locator('.conversation-management-layout');
  await expect(layout).toBeVisible();
  await expect(page.getByRole('complementary', { name: '工作区' })).toContainText('根工作区');
  await expect(page.getByRole('button', { name: '根工作区', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '最后活动时间', exact: true })).toContainText('全部时间');
  await expect(page.locator('.conversation-management-workspace-list small')).toHaveCount(0);
  await expect(page.locator('.conversation-management-table tbody td small')).toHaveCount(0);
  await expect(page.locator('.conversation-management-table tbody tr')).toHaveCount(20);
  await expect(page.getByRole('navigation', { name: '列表分页' })).toBeVisible();
  await expect(page.getByText('共 21 项，第 1 / 2 页')).toBeVisible();

  const [pageBox, pagerBox, tableBox] = await Promise.all([
    page.locator('.conversation-management-page').boundingBox(),
    page.getByRole('navigation', { name: '列表分页' }).boundingBox(),
    page.locator('.conversation-management-table-wrap').boundingBox(),
  ]);
  expect(pageBox).not.toBeNull();
  expect(pagerBox).not.toBeNull();
  expect(tableBox).not.toBeNull();
  expect((pagerBox?.y ?? 0) + (pagerBox?.height ?? 0)).toBeLessThanOrEqual((pageBox?.y ?? 0) + (pageBox?.height ?? 0));
  expect((tableBox?.y ?? 0) + (tableBox?.height ?? 0)).toBeLessThanOrEqual(pagerBox?.y ?? 0);
  await expect(page.locator('.conversation-management-page')).toHaveCSS('overflow-y', 'hidden');
  await expect(page.locator('.conversation-management-table-wrap')).toHaveCSS('overflow-y', 'auto');

  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.getByText('共 21 项，第 2 / 2 页')).toBeVisible();
  await expect(page.locator('.conversation-management-table tbody tr')).toHaveCount(1);
  await expect(page.getByText('会话 21', { exact: true })).toBeVisible();
});
