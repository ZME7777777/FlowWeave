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
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '导入插件', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '下一页' }).click();
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-11', exact: true })).toBeVisible();
  await dialog.getByLabel('搜索插件').fill('review-2');
  await dialog.getByRole('checkbox', { name: '选择插件 review-2', exact: true }).click();
  await dialog.getByRole('button', { name: /^导入插件/ }).click();
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
  await dialog.getByRole('button', { name: /^导入插件/ }).click();
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
  await dialog.getByRole('button', { name: /^导入插件/ }).click();
  await expect(dialog.getByRole('alert')).toContainText('分支链接不能固定版本');
  expect(requested).toBeUndefined();
  await dialog.getByLabel('仓库或提交链接').fill(`https://github.com/acme/tools/commit/${sha}`);
  await dialog.getByLabel('插件目录').fill('plugins/review');
  await dialog.getByRole('button', { name: /^导入插件/ }).click();
  await expect(dialog.getByRole('status')).toContainText('插件已导入能力仓库');
  expect(requested?.repo_path).toBe('plugins/review');
});

test('closing during resolution prevents late automatic publishing and ZIP remains accessible', async ({ page }) => {
  let finish!: () => void; const pending = new Promise<void>(resolve => { finish = resolve; });
  let publishCount = 0;
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => { await pending; await route.fulfill({ json: resolution }); });
  await page.route('**/api/v1/plugin-source-resolutions/plugin-resolution/publish', async route => { publishCount++; await route.fulfill({ json: { ...resolution, state: 'PUBLISHED' } }); });
  const dialog = await open(page);
  await dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true }).click();
  await dialog.getByRole('button', { name: /^导入插件/ }).click();
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
  await dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true }).click();
  const bounds = await dialog.boundingBox();
  expect(bounds).toBeTruthy(); expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  const row = await dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true }).boundingBox();
  expect(row!.height).toBeLessThan(70);
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: `../../.tmp/ops48/market-${width}.png` });
  await dialog.getByRole('tab', { name: 'Git 仓库' }).click();
  await dialog.getByText('如何获取导入链接？').click();
  await expect(dialog.getByRole('button', { name: '导入插件', exact: true })).toBeInViewport();
  await page.screenshot({ path: `../../.tmp/ops48/git-${width}.png` });
});


test('market list fills the dialog and keeps toggleable multiple selections across pages and search', async ({ page }) => {
  const requested: string[] = [];
  const published: string[] = [];
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => {
    const name = route.request().postDataJSON().plugin_name as string;
    requested.push(name);
    await route.fulfill({ json: { ...resolution, id: name } });
  });
  await page.route('**/api/v1/plugin-source-resolutions/*/publish', async route => {
    const name = new URL(route.request().url()).pathname.split('/').at(-2)!;
    published.push(name);
    await route.fulfill({ json: { ...resolution, id: name, state: 'PUBLISHED' } });
  });
  const dialog = await open(page);
  const first = dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true });
  await first.getByText('review-1', { exact: true }).click();
  await expect(first).toBeChecked();
  await first.locator('.plugin-import-check').click();
  await expect(first).not.toBeChecked();
  await first.press('Space');
  await expect(first).toBeChecked();
  await dialog.getByRole('button', { name: '下一页' }).click();
  await dialog.getByRole('checkbox', { name: '选择插件 review-11', exact: true }).click();
  await dialog.getByLabel('搜索插件').fill('review-2');
  await dialog.getByRole('checkbox', { name: '选择插件 review-2', exact: true }).click();
  await expect(dialog.getByText('已选择 3 个插件', { exact: true })).toBeVisible();
  await dialog.getByLabel('搜索插件').fill('');
  await expect(first).toBeChecked();
  await first.click();
  await expect(first).not.toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-2', exact: true })).toBeChecked();
  await expect(dialog.locator('.plugin-import-selection')).toHaveCount(0);
  for (const size of [{ width: 1440, height: 1000 }, { width: 800, height: 600 }]) {
    await page.setViewportSize(size);
    await expect.poll(() => dialog.evaluate(element => {
      const list = element.querySelector('.plugin-import-list')!;
      const pagination = element.querySelector('.plugin-import-pagination')!.getBoundingClientRect();
      const body = element.querySelector('.plugin-import-body')!;
      const bottomGap = body.getBoundingClientRect().bottom - pagination.bottom;
      const footerGap = element.querySelector('.plugin-import-footer')!.getBoundingClientRect().top - body.getBoundingClientRect().bottom;
      return { footerGap: Math.round(footerGap), bottomGap: Math.round(bottomGap), bodyOverflow: body.scrollHeight - body.clientHeight, listHeight: Math.round(list.getBoundingClientRect().height) };
    })).toMatchObject({ footerGap: 0, bottomGap: 16, bodyOverflow: 0 });
    expect(await dialog.locator('.plugin-import-list').evaluate(element => element.clientHeight)).toBeGreaterThan(180);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: '../../.tmp/ops53-plugin-multiselect/market-selection.png' });
  await dialog.getByRole('button', { name: '导入插件 (2)', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('2 个插件已导入能力仓库');
  expect(requested).toEqual(['review-11', 'review-2']);
  expect(published).toEqual(requested);
  await expect(dialog.getByRole('button', { name: '导入插件', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-2', exact: true })).toBeDisabled();
});

test('market batch retries only failed plugins and continues after a partial failure', async ({ page }) => {
  const requested: string[] = [];
  const published: string[] = [];
  let retry = false;
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => {
    const name = route.request().postDataJSON().plugin_name as string;
    requested.push(name); await route.fulfill({ json: { ...resolution, id: name } });
  });
  await page.route('**/api/v1/plugin-source-resolutions/*/publish', async route => {
    const name = new URL(route.request().url()).pathname.split('/').at(-2)!;
    published.push(name);
    await route.fulfill(name === 'review-2' && !retry
      ? { status: 503, json: { detail: '暂时无法发布' } }
      : { json: { ...resolution, id: name, state: 'PUBLISHED' } });
  });
  const dialog = await open(page);
  for (const name of ['review-1', 'review-2', 'review-3']) await dialog.getByRole('checkbox', { name: `选择插件 ${name}`, exact: true }).click();
  await dialog.getByRole('button', { name: '导入插件 (3)', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('review-2');
  await expect(dialog.getByRole('status')).toContainText('2 个插件已导入能力仓库');
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true })).not.toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-2', exact: true })).toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-3', exact: true })).toBeDisabled();
  retry = true;
  await dialog.getByRole('button', { name: '重试导入 (1)', exact: true }).click();
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await expect(dialog.getByRole('status')).toContainText('插件已导入能力仓库');
  expect(requested).toEqual(['review-1', 'review-2', 'review-3']);
  expect(published).toEqual(['review-1', 'review-2', 'review-3', 'review-2']);
});


test('closing a market batch preserves completed imports and stops the remaining queue', async ({ page }) => {
  let finish!: () => void;
  let delivered!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  const responseDelivered = new Promise<void>(resolve => { delivered = resolve; });
  const requested: string[] = [];
  const published: string[] = [];
  await page.route('**/api/v1/plugin-source-resolutions/marketplace', async route => {
    const name = route.request().postDataJSON().plugin_name as string;
    requested.push(name);
    if (name === 'review-2') await pending;
    await route.fulfill({ json: { ...resolution, id: name } });
    if (name === 'review-2') delivered();
  });
  await page.route('**/api/v1/plugin-source-resolutions/*/publish', async route => {
    const name = new URL(route.request().url()).pathname.split('/').at(-2)!;
    published.push(name);
    await route.fulfill({ json: { ...resolution, id: name, state: 'PUBLISHED' } });
  });
  const dialog = await open(page);
  for (const name of ['review-1', 'review-2', 'review-3']) await dialog.getByRole('checkbox', { name: `选择插件 ${name}`, exact: true }).click();
  await dialog.getByRole('button', { name: '导入插件 (3)', exact: true }).click();
  await expect.poll(() => requested).toEqual(['review-1', 'review-2']);
  await expect(dialog.getByRole('checkbox', { name: '选择插件 review-1', exact: true })).not.toBeChecked();
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  finish();
  await responseDelivered;
  await page.getByRole('button', { name: '关闭成功提示', exact: true }).click();
  await page.getByRole('button', { name: '导入 Plugin', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '导入 Plugin' }).getByRole('checkbox', { name: '选择插件 review-1', exact: true })).not.toBeChecked();
  expect(requested).toEqual(['review-1', 'review-2']);
  expect(published).toEqual(['review-1']);
});
