import { expect, test } from '@playwright/test';

test('double-clicking topbar whitespace toggles fullscreen without affecting navigation controls', async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'topbar-fullscreen-user', username: 'fullscreen-user', role: 'USER', is_super_admin: false }),
  }));
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.goto('/');

  const topbar = page.locator('.topbar');
  await expect(topbar).toHaveCSS('user-select', 'none');
  await topbar.dblclick({ position: { x: 8, y: 32 } });
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);

  await topbar.dblclick({ position: { x: 8, y: 32 } });
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);

  await page.getByRole('button', { name: '节点资产', exact: true }).dblclick();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);
});

test('brand control reloads current data without navigating or exiting fullscreen', async ({ page }) => {
  let nodeAssetRequests = 0;
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'topbar-reload-user', username: 'reload-user', role: 'USER', is_super_admin: false }),
  }));
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets', route => {
    nodeAssetRequests += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: '节点资产', exact: true })).toBeVisible();
  const topbar = page.locator('.topbar');
  await topbar.dblclick({ position: { x: 8, y: 32 } });
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations += 1; });

  await page.getByRole('button', { name: '重新加载当前页面数据' }).click();

  await expect.poll(() => nodeAssetRequests).toBeGreaterThanOrEqual(2);
  await expect(page).toHaveURL('/');
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);
  expect(navigations).toBe(0);
  await page.evaluate(() => document.exitFullscreen());
});

test('brand control reloads the selected page without changing its route', async ({ page }) => {
  let capabilityRequests = 0;
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'topbar-path-user', username: 'path-user', role: 'USER', is_super_admin: false }),
  }));
  await page.route('**/api/v1/capabilities', route => {
    capabilityRequests += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/v1/capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await expect(page.locator('.capabilities-page')).toBeVisible();

  await page.getByRole('button', { name: '重新加载当前页面数据' }).click();

  await expect.poll(() => capabilityRequests).toBeGreaterThanOrEqual(2);
  await expect(page).toHaveURL('/');
  await expect(page.locator('.capabilities-page')).toBeVisible();
});
