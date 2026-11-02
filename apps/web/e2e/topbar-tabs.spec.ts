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
  await page.mouse.move(700, 600);
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
  const menu = page.getByRole('menu', { name: '选择固定页面' });
  await trigger.hover(); await expect(menu).toBeVisible();
  await page.mouse.move(30, 500); await expect(menu).toHaveCount(0);
  await trigger.focus(); await page.keyboard.press('ArrowDown');
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
  for (const tab of await page.locator('.context-tab').all()) expect((await tab.boundingBox())!.width).toBeGreaterThanOrEqual(92);
  const lastTab = page.locator('.context-tab').last();
  const overflowBounds = (await overflowButton(page).boundingBox())!;
  const lastTabBounds = (await lastTab.boundingBox())!;
  expect(overflowBounds.x - (lastTabBounds.x + lastTabBounds.width)).toBeLessThanOrEqual(3);
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
  await expect(page.locator('.topbar-nav-boundary')).toHaveCount(1);
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
  await expect.poll(async () => (await visibleNames(page)).length).toBeGreaterThanOrEqual(3);
  await page.setViewportSize({ width: 1440, height: 850 });
  await expect(overflowButton(page)).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', { name: '选择固定页面' })).toBeVisible();
  await expect(page.locator('.context-tab.active')).toContainText('认证管理');
});

test('fixed navigation collapsed preference persists in localStorage across reloads', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page);
  await page.getByRole('button', { name: '收起固定导航' }).click();
  await expect.poll(() => page.evaluate(id => localStorage.getItem(`flowweave.topbar.fixed-collapsed.v1:${id}`), user.id)).toBe('true');
  await page.reload();
  await expect(page.getByRole('button', { name: '展开固定导航' })).toBeVisible();
  await page.getByRole('button', { name: '展开固定导航' }).click();
  await expect.poll(() => page.evaluate(id => localStorage.getItem(`flowweave.topbar.fixed-collapsed.v1:${id}`), user.id)).toBe('false');
  await page.reload();
  await expect(page.getByRole('button', { name: '收起固定导航' })).toBeVisible();
});

test('toggle stays visible without temporary tabs, tab widths follow titles and the fixed menu opens flush under its tab', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page, false);
  await expect(page.locator('.context-tabs')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '收起固定导航' })).toBeVisible();
  await page.getByRole('button', { name: '收起固定导航' }).click();
  const trigger = page.getByRole('button', { name: '选择固定页面' });
  await trigger.click();
  const menu = page.getByRole('menu', { name: '选择固定页面' });
  // The popover positions itself after mount and reveals with a clip-path animation, so assert the
  // final geometry with retries instead of sampling once.
  await expect.poll(async () => {
    const [t, m] = await Promise.all([trigger.boundingBox(), menu.boundingBox()]);
    return t && m ? [Math.round(Math.abs(m.width - t.width)), Math.round(Math.abs(m.y - (t.y + t.height)))] : [99, 99];
  }, { timeout: 5000 }).toEqual([0, 0]);
  await page.keyboard.press('Escape');
  await openSetting(page, '认证管理'); await openSetting(page, '终端环境');
  const widths = await page.locator('.context-tab').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().width));
  expect(widths.length).toBe(2);
  for (const width of widths) expect(width).toBeLessThan(168);
});

test('account settings menu preserves the topbar dark surface when opened', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page, false);
  const trigger = page.getByRole('button', { name: '账户与设置' });
  await trigger.click();
  const menu = page.getByRole('menu', { name: '账户与设置' });
  await expect(menu).toBeVisible();
  await expect(trigger).toHaveCSS('background-color', 'rgba(255, 255, 255, 0.05)');
  await expect(menu).toHaveCSS('background-color', 'rgb(20, 36, 30)');
  const [triggerBox, menuBox] = await Promise.all([trigger.boundingBox(), menu.boundingBox()]);
  expect(Math.abs(menuBox!.y - (triggerBox!.y + triggerBox!.height))).toBeLessThanOrEqual(1);
  await expect(menu.getByRole('menuitem', { name: '认证管理', exact: true })).toHaveCSS('color', 'rgb(207, 224, 213)');
});

test('hover menu stays open while the pointer crosses between tab and list, and closes after leaving', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route('**/api/v1/**', r => r.fulfill({ json: new URL(r.request().url()).pathname.endsWith('/auth/me') ? user : [] }));
  await page.goto('/');
  await page.getByRole('button', { name: '收起固定导航' }).click();
  await page.mouse.move(700, 600);
  const trigger = page.getByRole('button', { name: '选择固定页面' });
  const menu = page.getByRole('menu', { name: '选择固定页面' });
  await trigger.hover(); await expect(menu).toBeVisible();
  const t = (await trigger.boundingBox())!; const m = (await menu.boundingBox())!;
  await page.mouse.move(t.x + t.width / 2, t.y + t.height - 2, { steps: 4 });
  await page.mouse.move(m.x + m.width / 2, m.y + 40, { steps: 12 });
  await expect(menu).toBeVisible();
  await page.mouse.move(t.x + t.width / 2, t.y + t.height / 2, { steps: 8 });
  await page.waitForTimeout(700); await expect(menu).toBeVisible();
  await page.mouse.move(700, 600, { steps: 6 }); await expect(menu).toHaveCount(0);
});
