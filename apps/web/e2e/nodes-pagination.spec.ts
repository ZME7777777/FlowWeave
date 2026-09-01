import { expect, test } from '@playwright/test';

test('desktop node assets keep a full five-by-five grid on the first page', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  const nodes = Array.from({ length: 25 }, (_, index) => ({
    id: `node-${index}`,
    directory_id: null,
    name: `节点 ${index + 1}`,
    description: '分页回归验证',
    icon_kind: 'TEXT',
    icon_value: 'NO',
    row_version: 1,
    inputs: [],
    outputs: [],
    executor: null,
    context_capabilities: [],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  }));
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(nodes),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();

  await expect(page.getByTestId('node-card')).toHaveCount(25);
  await expect(page.getByRole('navigation', { name: '列表分页' })).toHaveCount(0);
});
