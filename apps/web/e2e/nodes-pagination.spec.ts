import { expect, test } from '@playwright/test';

test('prefixed deployment keeps API requests under the application base path when API base is empty', async ({ page }) => {
  test.skip(process.env.E2E_PREFIX_DEPLOYMENT !== '1', 'requires a Vite server started with the prefixed deployment environment');
  const node = {
    id: 'node-prefix-regression', directory_id: null, name: '前缀路由节点', description: '',
    icon_kind: 'TEXT', icon_value: 'PR', row_version: 1, inputs: [], outputs: [], executor: null,
    context_capabilities: [], created_at: '2026-09-02T00:00:00Z', updated_at: '2026-09-02T00:00:00Z',
  };
  const requestedPaths: string[] = [];
  await page.route('**/flowweave/api/v1/node-directories', route => {
    requestedPaths.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/flowweave/api/v1/node-assets', route => {
    requestedPaths.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([node]) });
  });

  await page.goto('/flowweave/');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();

  await expect(page.getByTestId('node-card')).toHaveCount(1);
  expect(requestedPaths).toEqual(expect.arrayContaining([
    '/flowweave/api/v1/node-directories',
    '/flowweave/api/v1/node-assets',
  ]));
});

test('desktop node assets keep a full five-by-five grid on the first page', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  const nodes = Array.from({ length: 25 }, (_, index) => ({
    id: `node-${index}`,
    directory_id: null,
    name: `节点 ${index + 1}`,
    description: index === 0
      ? '这是用于验证紧凑卡片不会被长说明撑开或跨越相邻卡片的超长节点说明，必绑能力：exception-baseline-publisher、lark-sheets、find-and-pull-hq-git。'
      : '分页回归验证',
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

  const firstCard = page.getByTestId('node-card').first();
  const summary = firstCard.locator('.node-list-summary p');
  const geometry = await firstCard.evaluate(card => {
    const paragraph = card.querySelector('.node-list-summary p');
    if (!(paragraph instanceof HTMLElement)) throw new Error('node summary is missing');
    const cardRect = card.getBoundingClientRect();
    const paragraphRect = paragraph.getBoundingClientRect();
    return {
      cardRight: cardRect.right,
      paragraphRight: paragraphRect.right,
      cardHeight: cardRect.height,
      paragraphHeight: paragraphRect.height,
      scrollWidth: paragraph.scrollWidth,
      clientWidth: paragraph.clientWidth,
    };
  });
  expect(geometry.paragraphRight).toBeLessThanOrEqual(geometry.cardRight);
  expect(geometry.paragraphHeight).toBeLessThan(geometry.cardHeight);
  expect(geometry.scrollWidth).toBeGreaterThan(geometry.clientWidth);
  await expect(summary).toHaveCSS('white-space', 'nowrap');
  await expect(summary).toHaveCSS('text-overflow', 'ellipsis');
});
