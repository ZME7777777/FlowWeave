import { expect, test, type Route } from '@playwright/test';

async function respond(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('keeps account settings in unique temporary tabs and closes the active tab predictably', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) {
      return respond(route, {
        id: 'temporary-tab-admin', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true,
        can_use_agent_sessions: true,
      });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await expect(page.locator('.context-tabs')).toHaveCount(0);

  const openSettings = async (name: string) => {
    await page.getByRole('button', { name: '账户与设置' }).click();
    await page.getByRole('menuitem', { name, exact: true }).click();
  };

  await openSettings('终端环境');
  await openSettings('认证管理');
  await openSettings('大模型配置');
  await openSettings('用户管理');
  await expect(page.getByLabel('临时管理标签')).toBeVisible();
  await expect(page.locator('.context-tab')).toHaveCount(4);

  await openSettings('大模型配置');
  await expect(page.locator('.context-tab')).toHaveCount(4);
  await expect(page.locator('.context-tab.active')).toContainText('大模型配置');

  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await expect(page.getByLabel('临时管理标签')).toBeVisible();
  await expect(page.locator('.context-tab')).toHaveCount(4);

  await page.getByRole('button', { name: '大模型配置', exact: true }).click();
  await page.getByRole('button', { name: '关闭大模型配置', exact: true }).click();
  await expect(page.locator('.context-tab')).toHaveCount(3);
  await expect(page.locator('.context-tab.active')).toContainText('用户管理');

  for (const label of ['用户管理', '认证管理', '终端环境']) {
    await page.getByRole('button', { name: `关闭${label}`, exact: true }).click();
  }
  await expect(page.locator('.context-tabs')).toHaveCount(0);
});
