import { expect, test, type Page } from '@playwright/test';

const sha = 'a'.repeat(40);
const catalog = { schema_version: 1, source: 'https://github.com/OpenHands/extensions.git', commit: sha,
  marketplace_name: 'OpenHands', owner: 'OpenHands', plugins: Array.from({ length: 11 }, (_, i) => ({
    name: `review-${i + 1}`, description: `审查代码、执行验证并生成可读的变更建议 ${i + 1}`, version: '1.0.0', author: 'OpenHands',
  })),
};
const resolution = { id: 'plugin-resolution', source_kind: 'GIT', source_url: 'https://github.com/acme/tools',
  requested_commit: sha, state: 'READY', state_version: 2, preview: {}, expires_at: '2026-10-29T00:00:00Z' };
async function open(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Plugin/ }).click();
  await page.getByRole('button', { name: '导入 Plugin', exact: true }).click();
  return page.getByRole('dialog', { name: '导入 Plugin' });
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({ json: { id: 'plugin-user', username: 'plugin-user', role: 'USER' } }));
  await page.route('**/api/v1/capability-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20, counts: {}, collection_total: 0 } }));
  await page.route('**/api/v1/capability-collection-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } }));
  await page.route('**/api/v1/plugin-marketplace-catalogs/openhands', route => route.fulfill({ json: catalog }));
});

test('market loading, retry, search and import use a single compact content dialog', async ({ page }) => {
  let failBrowse = true;
  await page.route('**/api/v1/plugin-marketplace-catalogs/openhands', async route => {
    const fail = failBrowse;
    await new Promise(resolve => setTimeout(resolve, 350));
    await route.fulfill(fail ? { status: 503, json: { detail: '目录暂时不可用' } } : { json: catalog });
  });
  let requested: Record<string, unknown> | undefined;
  let publishCount = 0;
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => {
    requested = route.request().postDataJSON(); await route.fulfill({ json: { ...resolution, state: 'PENDING', state_version: 1 } });
  });
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution', route => route.fulfill({ json: resolution }));
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution/publish', async route => {
    publishCount++; expect(route.request().postDataJSON()).toEqual({ expected_state_version: 2 });
    await route.fulfill({ json: { ...resolution, state: 'PUBLISHED' } });
  });
  const dialog = await open(page);
  await expect(dialog.getByText('正在加载插件目录…')).toBeVisible();
  await expect(dialog.getByText('暂时无法加载插件市场')).toBeVisible();
  failBrowse = false;
  await dialog.getByRole('button', { name: '重新加载' }).click();
  await expect(dialog.getByRole('button', { name: /review-1 / })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '导入插件', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '下一页' }).click();
  await expect(dialog.getByRole('button', { name: /review-11 / })).toBeVisible();
  await dialog.getByLabel('搜索插件').fill('review-2');
  await dialog.getByRole('button', { name: /review-2 / }).click();
  await dialog.getByRole('button', { name: '导入插件', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('插件已导入能力仓库');
  expect(requested).toEqual({ marketplace_source_url: catalog.source, marketplace_commit: sha, plugin_name: 'review-2' });
  expect(publishCount).toBe(1);
});

test('Git fixed directory links fill immutable source and failed publishing can retry without resolving again', async ({ page }) => {
  let requested: Record<string, unknown> | undefined;
  let resolveCount = 0; let publishCount = 0;
  await page.route('**/api/v1/plugin-source-resolutions', async route => {
    resolveCount++; requested = route.request().postDataJSON(); await route.fulfill({ json: resolution });
  });
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution/publish', async route => {
    publishCount++;
    await route.fulfill(publishCount === 1 ? { status: 503, json: { detail: '请重试' } } : { json: { ...resolution, state: 'PUBLISHED' } });
  });
  const dialog = await open(page);
  await dialog.getByRole('tab', { name: 'Git 仓库' }).click();
  await dialog.getByText('如何获取导入链接？').click();
  await expect(dialog.getByText(/只有 SKILL.md 的技能/)).toBeVisible();
  await dialog.getByLabel('仓库或提交链接').fill(`https://github.com/acme/tools/tree/${sha}/plugins/review`);
  await dialog.getByRole('button', { name: '导入插件', exact: true }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  expect(requested).toEqual({ source_url: 'https://github.com/acme/tools', commit: sha, repo_path: 'plugins/review' });
  await expect(dialog.getByLabel('完整提交号')).toHaveValue(sha);
  await dialog.getByRole('button', { name: '重试导入' }).click();
  await expect(dialog.getByRole('status')).toContainText('插件已导入能力仓库');
  expect(resolveCount).toBe(1); expect(publishCount).toBe(2);
});

test('Git guidance rejects mutable branches and supports commit links with a manual subdirectory', async ({ page }) => {
  let requested: Record<string, unknown> | undefined;
  await page.route('**/api/v1/plugin-source-resolutions', async route => { requested = route.request().postDataJSON(); await route.fulfill({ json: resolution }); });
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution/publish', route => route.fulfill({ json: { ...resolution, state: 'PUBLISHED' } }));
  const dialog = await open(page);
  await dialog.getByRole('tab', { name: 'Git 仓库' }).click();
  await dialog.getByLabel('仓库或提交链接').fill('https://github.com/acme/tools/tree/main');
  await dialog.getByRole('button', { name: '导入插件', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('分支链接不能固定版本');
  expect(requested).toBeUndefined();
  await dialog.getByLabel('仓库或提交链接').fill(`https://github.com/acme/tools/commit/${sha}`);
  await dialog.getByLabel('插件目录').fill('plugins/review');
  await dialog.getByRole('button', { name: '导入插件', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('插件已导入能力仓库');
  expect(requested?.repo_path).toBe('plugins/review');
});

test('closing during resolution prevents late automatic publishing and ZIP remains accessible', async ({ page }) => {
  let finish!: () => void; const pending = new Promise<void>(resolve => { finish = resolve; });
  let publishCount = 0;
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => { await pending; await route.fulfill({ json: resolution }); });
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution/publish', async route => { publishCount++; await route.fulfill({ json: { ...resolution, state: 'PUBLISHED' } }); });
  const dialog = await open(page);
  await dialog.getByRole('button', { name: /review-1 / }).click();
  await dialog.getByRole('button', { name: '导入插件', exact: true }).click();
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  finish();
  await page.getByRole('button', { name: '导入 Plugin', exact: true }).click();
  const fileChooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: '上传 ZIP' }).click();
  await (await fileChooser).setFiles([]);
  expect(publishCount).toBe(0);
});

for (const width of [1440, 390]) test(`Plugin dialog keeps compact rows and controls in bounds at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const dialog = await open(page);
  await dialog.getByRole('button', { name: /review-1 / }).click();
  const bounds = await dialog.boundingBox();
  expect(bounds).toBeTruthy(); expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  const row = await dialog.getByRole('button', { name: /review-1 / }).boundingBox();
  expect(row!.height).toBeLessThan(70);
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: `../../.tmp/ops48/market-${width}.png` });
  await dialog.getByRole('tab', { name: 'Git 仓库' }).click();
  await dialog.getByText('如何获取导入链接？').click();
  await expect(dialog.getByRole('button', { name: '导入插件', exact: true })).toBeInViewport();
  await page.screenshot({ path: `../../.tmp/ops48/git-${width}.png` });
});
