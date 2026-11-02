import { test, expect } from '@playwright/test';
const user = { id: 'u', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
test('hover keeps menu open while crossing to it, click toggles, leaving closes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route('**/api/v1/**', r => r.fulfill({ json: new URL(r.request().url()).pathname.endsWith('/auth/me') ? user : [] }));
  await page.goto('/');
  await page.getByRole('button', { name: '收起固定导航' }).click();
  const trigger = page.getByRole('button', { name: '选择固定页面' });
  const menu = page.getByRole('menu', { name: '选择固定页面' });
  await trigger.hover(); await expect(menu).toBeVisible();
  const t = (await trigger.boundingBox())!; const m = (await menu.boundingBox())!;
  // slow diagonal move from the trigger into the menu body
  await page.mouse.move(t.x + t.width / 2, t.y + t.height - 2, { steps: 4 });
  await page.mouse.move(m.x + m.width / 2, m.y + 40, { steps: 12 });
  await expect(menu).toBeVisible();
  // moving back up onto the trigger must not close it either
  await page.mouse.move(t.x + t.width / 2, t.y + t.height / 2, { steps: 8 });
  await page.waitForTimeout(400); await expect(menu).toBeVisible();
  // click on the trigger toggles it closed
  await trigger.click(); await expect(menu).toHaveCount(0);
  // hovering away closes after the grace period
  await trigger.hover(); await expect(menu).toBeVisible();
  await page.mouse.move(700, 600, { steps: 6 }); await expect(menu).toHaveCount(0);
});
