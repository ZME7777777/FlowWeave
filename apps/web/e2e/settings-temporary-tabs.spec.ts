import { expect, test, type Page, type Route } from '@playwright/test';

const admin = {
  id: 'temporary-tab-admin', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true,
  can_use_agent_sessions: true,
};

async function openSettings(page: Page, name: string) {
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name, exact: true }).click();
}

async function mockSettingsApi(page: Page, getUser = () => admin) {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me') || path.endsWith('/auth/login')) return respond(route, getUser());
    if (path.endsWith('/auth/logout')) return route.fulfill({ status: 204 });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
}

async function respond(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('keeps account settings in unique temporary tabs and closes the active tab predictably', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await mockSettingsApi(page);

  await page.goto('/');
  await expect(page.locator('.context-tabs')).toHaveCount(0);

  await openSettings(page, '终端环境');
  await openSettings(page, '认证管理');
  await openSettings(page, '大模型配置');
  await openSettings(page, '用户管理');
  await expect(page.getByLabel('临时管理标签')).toBeVisible();
  await expect(page.locator('.context-tab')).toHaveCount(4);

  await openSettings(page, '大模型配置');
  await expect(page.locator('.context-tab')).toHaveCount(4);
  await expect(page.locator('.context-tab.active')).toContainText('大模型配置');

  await page.reload();
  await expect(page.locator('.context-tab')).toHaveText(['用户管理', '大模型配置', '认证管理', '终端环境']);
  await expect(page.locator('.context-tab.active')).toContainText('大模型配置');

  await openSettings(page, '用户管理');
  await expect(page.locator('.context-tab')).toHaveCount(4);

  // Temporary tabs share the permanent nav tab's anatomy: full topbar
  // height, label size and the lime underline marking the active tab.
  const permanent = page.getByRole('button', { name: '节点中心', exact: true });
  const temporary = page.locator('.context-tab.active');
  const [permanentBox, temporaryBox] = await Promise.all([permanent.boundingBox(), temporary.boundingBox()]);
  expect(temporaryBox?.height).toBe(permanentBox?.height);
  await expect(temporary.locator('.context-tab-select')).toHaveCSS('font-size', await permanent.evaluate(element => getComputedStyle(element).fontSize));
  await expect(temporary).toHaveCSS('border-bottom-color', 'rgb(216, 242, 86)');
  await expect(temporary).toHaveCSS('border-top-left-radius', '0px');

  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await expect(page.getByLabel('临时管理标签')).toBeVisible();
  await expect(page.locator('.context-tab')).toHaveCount(4);
  await page.reload();
  await expect(page.locator('.context-tab')).toHaveText(['用户管理', '大模型配置', '认证管理', '终端环境']);
  await expect(page.locator('.context-tab.active')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '节点中心', exact: true })).toHaveClass('active');

  await page.getByRole('button', { name: '大模型配置', exact: true }).click();
  await page.getByRole('button', { name: '关闭大模型配置', exact: true }).click();
  await expect(page.locator('.context-tab')).toHaveCount(3);
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');
  await page.reload();
  await expect(page.locator('.context-tab')).toHaveText(['用户管理', '认证管理', '终端环境']);
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');

  for (const label of ['用户管理', '认证管理', '终端环境']) {
    await page.getByRole('button', { name: `关闭${label}`, exact: true }).click();
  }
  await expect(page.locator('.context-tabs')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.context-tabs')).toHaveCount(0);
});

test('isolates saved tabs by account and filters settings after permission changes', async ({ page }) => {
  let user = { ...admin };
  await mockSettingsApi(page, () => user);
  await page.goto('/');
  await openSettings(page, '大模型配置');
  await openSettings(page, '用户管理');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();

  user = { ...admin, id: 'other-user', username: 'user', role: 'USER', is_super_admin: false };
  await page.reload();
  await expect(page.getByRole('button', { name: '账户与设置' })).toContainText('user');
  await expect(page.locator('.context-tabs')).toHaveCount(0);

  user = { ...admin };
  await page.reload();
  await expect(page.locator('.context-tab')).toHaveText(['用户管理', '大模型配置']);
  await page.getByRole('button', { name: '用户管理', exact: true }).click();
  user = { ...admin, role: 'USER', is_super_admin: false };
  await page.reload();
  await expect(page.locator('.context-tab')).toHaveText(['大模型配置']);
  await expect(page.getByRole('button', { name: '节点中心', exact: true })).toHaveClass('active');
});

test('logout clears temporary tabs before the next login', async ({ page }) => {
  await mockSettingsApi(page);
  await page.goto('/');
  await openSettings(page, '终端环境');
  await openSettings(page, '大模型配置');
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录 FlowWeave' })).toBeVisible();
  await page.getByLabel('账号').fill('flowweave');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByRole('button', { name: '账户与设置' })).toBeVisible();
  await expect(page.locator('.context-tabs')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.context-tabs')).toHaveCount(0);
});

for (const storage of ['corrupt', 'unavailable'] as const) {
  test(`temporary tabs remain usable with ${storage} session storage`, async ({ page }) => {
    await mockSettingsApi(page);
    await page.addInitScript(mode => {
      if (mode === 'corrupt') sessionStorage.setItem('flowweave.context-tabs.v1:temporary-tab-admin', '{invalid');
      else {
        const getItem = Storage.prototype.getItem;
        const setItem = Storage.prototype.setItem;
        Storage.prototype.getItem = function (key) {
          if (key.startsWith('flowweave.context-tabs.')) throw new Error('storage disabled');
          return getItem.call(this, key);
        };
        Storage.prototype.setItem = function (key, value) {
          if (key.startsWith('flowweave.context-tabs.')) throw new Error('storage disabled');
          return setItem.call(this, key, value);
        };
      }
    }, storage);
    await page.goto('/');
    await expect(page.locator('.context-tabs')).toHaveCount(0);
    await openSettings(page, '终端环境');
    await openSettings(page, '大模型配置');
    await expect(page.locator('.context-tab')).toHaveText(['大模型配置', '终端环境']);
    await page.getByRole('button', { name: '关闭终端环境' }).click();
    await expect(page.locator('.context-tab.active')).toHaveText('大模型配置');
    await page.getByRole('button', { name: '关闭大模型配置' }).click();
    await expect(page.locator('.context-tabs')).toHaveCount(0);
  });
}
