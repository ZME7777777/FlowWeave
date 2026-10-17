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

test('brand control clears browser-managed site data before reloading the current page', async ({ page }) => {
  let authRequests = 0;
  await page.route('**/api/v1/auth/me', route => {
    authRequests += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'topbar-reload-user', username: 'reload-user', role: 'USER', is_super_admin: false }),
    });
  });
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.goto('/');
  await expect(page.getByRole('button', { name: '节点资产', exact: true })).toBeVisible();
  await page.evaluate(async () => {
    localStorage.setItem('reload-local-probe', 'present');
    sessionStorage.setItem('reload-session-probe', 'present');
    await caches.open('reload-cache-probe');
  });

  const reloaded = page.waitForEvent('framenavigated', frame => frame === page.mainFrame());
  await page.getByRole('button', { name: '清除缓存并刷新页面' }).click();
  await reloaded;

  await expect.poll(() => authRequests).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: '节点资产', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(async () => ({
    local: localStorage.getItem('reload-local-probe'),
    session: sessionStorage.getItem('reload-session-probe'),
    cacheExists: (await caches.keys()).includes('reload-cache-probe'),
  }))).toEqual({ local: null, session: null, cacheExists: false });
});

test('brand control restores the selected page after clearing cache', async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'topbar-path-user', username: 'path-user', role: 'USER', is_super_admin: false }),
  }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await expect(page.locator('.capabilities-page')).toBeVisible();

  await page.getByRole('button', { name: '清除缓存并刷新页面' }).click();

  await expect(page).toHaveURL('/');
  await expect(page.locator('.capabilities-page')).toBeVisible();
});
