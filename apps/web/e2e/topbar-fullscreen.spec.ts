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
  await topbar.dblclick({ position: { x: 8, y: 32 } });
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);

  await topbar.dblclick({ position: { x: 8, y: 32 } });
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);

  await page.getByRole('button', { name: '节点资产', exact: true }).dblclick();
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);
});
