import { expect, test, type Page } from '@playwright/test';

const user = { id: 'topbar-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const settings = ['environments', 'credentials', 'models', 'ldap-users'];
const labels = ['终端环境', '认证管理', '大模型配置', '用户管理'];
async function setup(page: Page, seed = true) {
  await page.addInitScript(({ userId, views, seed }) => {
    if (seed && !sessionStorage.getItem(`flowweave.context-tabs.v2:${userId}`)) sessionStorage.setItem(`flowweave.context-tabs.v2:${userId}`, JSON.stringify([...views].reverse().map(view => ({ kind: 'settings', view }))));
  }, { userId: user.id, views: settings, seed });
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ json: user });
    if (path.endsWith('/capability-directory')) return route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20, counts: {}, collection_total: 0 } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: '账户与设置' })).toBeVisible();
}
const visibleNames = (page: Page) => page.locator('.context-tab-select>span').allTextContents();
const overflowButton = (page: Page) => page.getByRole('button', { name: /^其他临时标签/ });
async function openSetting(page: Page, name: string) {
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name, exact: true }).click();
}

test('fixed navigation collapses smoothly to one custom menu and expands without resetting page state', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page);
  const region = page.locator('.topbar-fixed-region');
  const before = (await region.boundingBox())!.width;
  await page.getByRole('button', { name: '收起固定导航' }).click();
  await expect.poll(async () => (await region.boundingBox())!.width).toBe(132);
  expect(before).toBeGreaterThan(400);
  await expect(page.getByRole('navigation', { name: '主导航' })).toHaveCount(0);
  const trigger = page.getByRole('button', { name: '选择固定页面' });
  await trigger.focus(); await page.keyboard.press('ArrowDown');
  const menu = page.getByRole('menu', { name: '选择固定页面' });
  await expect(menu.getByRole('menuitem')).toHaveCount(7);
  await page.keyboard.press('End'); await expect(menu.getByRole('menuitem', { name: 'AI 工作台' })).toBeFocused();
  await page.keyboard.press('Escape'); await expect(menu).toHaveCount(0); await expect(trigger).toBeFocused();
  await trigger.click(); await menu.getByRole('menuitem', { name: '能力仓库', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '搜索当前能力模块' })).toBeVisible();
  await page.getByRole('textbox', { name: '搜索当前能力模块' }).fill('保留筛选状态');
  await page.getByRole('button', { name: '展开固定导航' }).click();
  await expect.poll(async () => (await region.boundingBox())!.width).toBeCloseTo(before, 0);
  await expect(page.getByRole('textbox', { name: '搜索当前能力模块' })).toHaveValue('保留筛选状态');
  expect(await page.locator('select').count()).toBe(0);
  await page.screenshot({ path: '../../.tmp/ops82/expanded.png' });
});

test('overflow keeps full tab widths, promotes a hidden item left and backfills after closing', async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 850 });
  await setup(page);
  await expect(overflowButton(page)).toBeVisible();
  const initial = await visibleNames(page);
  expect(initial[0]).toBe('用户管理');
  for (const tab of await page.locator('.context-tab').all()) expect((await tab.boundingBox())!.width).toBe(168);
  await overflowButton(page).hover();
  let menu = page.getByRole('menu', { name: /^其他临时标签/ });
  await expect(menu.getByRole('menuitem', { name: '终端环境', exact: true })).toBeVisible();
  const hiddenBefore = await menu.getByRole('menuitem').allTextContents();
  await menu.getByRole('menuitem', { name: '终端环境', exact: true }).click();
  await expect(page.locator('.context-tab-select').first()).toHaveText('终端环境');
  await expect(page.locator('.context-tab.active')).toContainText('终端环境');
  expect(await visibleNames(page)).toHaveLength(initial.length);
  await overflowButton(page).hover(); menu = page.getByRole('menu', { name: /^其他临时标签/ });
  await expect(menu.getByRole('menuitem', { name: initial.at(-1)!, exact: true })).toBeVisible();
  await page.mouse.move(30, 500); await expect(menu).toHaveCount(0);
  await page.getByRole('button', { name: '关闭终端环境', exact: true }).click();
  await expect.poll(() => visibleNames(page)).toEqual(initial);
  expect(hiddenBefore.length).toBeGreaterThan(0);
  await page.getByRole('button', { name: '收起固定导航' }).click();
  await expect.poll(() => visibleNames(page)).toHaveLength(4 - 1);
  await expect(overflowButton(page)).toHaveCount(0);
  await expect.poll(async () => (await page.locator('.topbar-fixed-region').boundingBox())!.width).toBe(132);
  await page.screenshot({ path: '../../.tmp/ops82/collapsed.png' });
  expect(await page.locator('.topbar').evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
});

test('narrow menus stay within the viewport and closing tabs drains the hidden list', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await setup(page);
  await overflowButton(page).hover();
  const menu = page.getByRole('menu', { name: /^其他临时标签/ });
  await expect(menu).toBeVisible();
  const bounds = (await menu.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(8);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(1016);
  await page.screenshot({ path: '../../.tmp/ops82/overflow.png' });
  await page.locator('.topbar').click({ position: { x: 4, y: 4 } });
  await expect(menu).toHaveCount(0);
  await overflowButton(page).focus(); await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  await expect(overflowButton(page)).toBeFocused();
  const closed: string[] = [];
  for (let remaining = 4; remaining > 0; remaining--) {
    const name = (await visibleNames(page))[0];
    expect(closed).not.toContain(name);
    closed.push(name);
    await page.getByRole('button', { name: `关闭${name}`, exact: true }).focus();
    await page.keyboard.press('Enter');
    if (remaining > 1) {
      await expect(page.locator('.context-tab-select').first()).not.toHaveText(name);
      await expect(page.locator('.context-tab-select').first()).toBeFocused();
    }
  }
  await expect(page.locator('.context-tabs')).toHaveCount(0);
  await expect(page.locator('.topbar-nav-boundary')).toHaveCount(0);
});

test('new tabs win space and hidden pages stay available through keyboard and account menu', async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 850 });
  await setup(page, false);
  for (const label of labels) await openSetting(page, label);
  await expect(page.locator('.context-tab-select').first()).toHaveText('用户管理');
  await overflowButton(page).focus(); await page.keyboard.press('ArrowDown');
  const menu = page.getByRole('menu', { name: /^其他临时标签/ });
  await expect(menu.getByRole('menuitem').first()).toBeFocused();
  await page.keyboard.press('End'); await page.keyboard.press('Enter');
  await expect(page.locator('.context-tab-select').first()).toHaveText('终端环境');
  await openSetting(page, '认证管理');
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');
  await page.reload();
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');
  await page.getByRole('button', { name: '收起固定导航' }).click();
  await expect.poll(() => visibleNames(page)).toHaveLength(3);
  await page.setViewportSize({ width: 1440, height: 850 });
  await expect(overflowButton(page)).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', { name: '选择固定页面' })).toBeVisible();
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');
});
