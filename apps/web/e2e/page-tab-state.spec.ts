import { expect, test, type Page } from '@playwright/test';

async function openSettings(page: Page, name: string) {
  await page.getByRole('button', { name: '账户与设置', exact: true }).click();
  await page.getByRole('menuitem', { name, exact: true }).click();
}

async function setup(page: Page) {
  const requests: string[] = [];
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path.endsWith('/auth/me')) return json({ id: 'tab-user', username: 'tab-user', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true });
    if (path.endsWith('/capability-directory')) return json({ items: [], total: 0, page: 1, page_size: 20, counts: {}, collection_total: 0 });
    if (path.endsWith('/capability-collection-directory')) return json({ items: [], total: 0, page: 1, page_size: 20 });
    if (path.endsWith('/website-credentials')) return json(Array.from({ length: 25 }, (_, index) => ({ id: `credential-${index}`, name: `认证 ${index}`, target_host: 'example.test', target_path: '/', auth_type: 'TOKEN', include_subdomains: false, has_secret: true, row_version: 1 })));
    return json([]);
  });
  await page.goto('/');
  return requests;
}

test('main and settings tabs restore filters and exact page DOM while closing a tab releases its state', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await page.getByLabel('搜索流程', { exact: true }).fill('保留的流程条件');
  await page.getByLabel('搜索流程', { exact: true }).evaluate(element => { element.dataset.retainedMarker = 'same-dom'; });
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
  await page.getByLabel('搜索当前能力模块').fill('保留的 Context 条件');
  await openSettings(page, '认证管理');
  await page.getByLabel('搜索认证管理').fill('认证 2');
  await page.getByRole('checkbox', { name: '选择 认证 20', exact: true }).check();
  await page.getByRole('button', { name: '查看 认证 20', exact: true }).click();
  await openSettings(page, '大模型配置');
  await page.getByLabel('搜索大模型配置').fill('保留的模型条件');

  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await expect(page.getByLabel('搜索流程', { exact: true })).toHaveValue('保留的流程条件');
  await expect(page.getByLabel('搜索流程', { exact: true })).toHaveAttribute('data-retained-marker', 'same-dom');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await expect(page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByLabel('搜索当前能力模块')).toHaveValue('保留的 Context 条件');
  await page.getByRole('button', { name: '认证管理', exact: true }).click();
  await expect(page.getByLabel('搜索认证管理')).toHaveValue('认证 2');
  await expect(page.getByRole('checkbox', { name: '选择 认证 20', exact: true })).toBeChecked();
  await expect(page.getByRole('main', { name: '认证管理详情' }).getByRole('heading', { name: '认证 20', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '大模型配置', exact: true }).click();
  await expect(page.getByLabel('搜索大模型配置')).toHaveValue('保留的模型条件');
  await page.getByRole('button', { name: '关闭大模型配置', exact: true }).click();
  await openSettings(page, '大模型配置');
  await expect(page.getByLabel('搜索大模型配置')).toHaveValue('');
  expect(await page.locator('[data-page-tab]').evaluateAll(nodes => nodes.filter(node => getComputedStyle(node).display !== 'none').length)).toBe(1);
});

test('hidden schedule tabs stop polling and resume when shown', async ({ page }) => {
  await page.clock.install();
  const requests = await setup(page);
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  const count = () => requests.filter(path => path.endsWith('/flow-run-schedules')).length;
  await expect.poll(count).toBeGreaterThan(0);
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  const before = count();
  await page.clock.fastForward(20_000);
  expect(count()).toBe(before);
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  await expect.poll(count).toBeGreaterThan(before);
});


test('flow editor preserves unsaved graph nodes when its tab is hidden and restored', async ({ page }) => {
  await setup(page);
  const asset = { id: 'retained-asset', name: '待编排节点', directory_id: null, inputs: [], outputs: [], executor: { startup_prompt: '', context_prompt: '', context_capability_ids: [] } };
  const flow = { id: 'retained-flow', name: '保留编排', description: '', nodes: [], edges: [], port_mappings: [], row_version: 1 };
  await page.route('**/api/v1/node-assets**', route => route.fulfill({ json: [asset] }));
  await page.route('**/api/v1/flows', route => route.fulfill({ json: [flow] }));
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await page.getByRole('button', { name: flow.name, exact: true }).dblclick();
  await page.getByTestId('flow-library').getByRole('button', { name: asset.name, exact: true }).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(1);
  await page.getByRole('button', { name: '产物流转', exact: true }).click();
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await expect(page.getByRole('button', { name: '返回全部流程' })).toBeVisible();
  await expect(page.locator('.react-flow__node')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '产物流转', exact: true })).toHaveAttribute('aria-pressed', 'true');
});

test('settings tabs retain button pagination, selected records and independent list scrolling', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 650 });
  await setup(page);
  await openSettings(page, '认证管理');
  const list = page.getByRole('complementary', { name: '认证管理列表' });
  await list.getByRole('button', { name: '下一页', exact: true }).click();
  await list.getByRole('button', { name: '查看 认证 24', exact: true }).click();
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: '认证管理', exact: true }).click();
  await expect(list.locator('.settings-resource-row')).toHaveCount(5);
  await expect(page.getByRole('main', { name: '认证管理详情' }).getByRole('heading', { name: '认证 24', exact: true })).toBeVisible();
  await list.getByRole('button', { name: '上一页', exact: true }).click();
  await list.locator('.settings-management-list-scroll').evaluate(element => { element.scrollTop = 300; });
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: '认证管理', exact: true }).click();
  await expect.poll(() => list.locator('.settings-management-list-scroll').evaluate(element => element.scrollTop)).toBe(300);
});
