import { expect, test, type Page } from '@playwright/test';

const items = ['localCode', 'codeGraph', 'flowweave-reference-history'].map((name, index) => ({
  id: `mcp-${index}`, lineage_id: `lineage-${index}`, capability_key: name, capability_type: 'MCP',
  revision_number: index === 2 ? 2 : 1, is_latest: true, is_builtin: index === 2,
  description: 'MCP 连接测试', filename: 'mcp.json', byte_size: 200, created_at: '2026-10-29T09:00:00Z',
  reference_count: 0, document: { transport: 'streamable-http', url: 'https://mcp.example.test' },
  dependencies: {}, dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null,
}));

async function setup(page: Page, probe: (id: string) => Promise<unknown>, history: typeof items = []) {
  const calls: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ json: { id: 'mcp-user', username: 'mcp-user', role: 'SUPER_ADMIN', is_super_admin: true } });
    if (path.endsWith('/capability-directory')) return route.fulfill({ json: { items: items.map(item => ({ id: item.lineage_id, latest: item, versions: [item, ...history.filter(version => version.lineage_id === item.lineage_id)] })), total: items.length, page: 1, page_size: 20, counts: { MCP: 3 }, collection_total: 0 } });
    if (path.endsWith('/capabilities')) return route.fulfill({ json: [...items, ...history] });
    if (path.endsWith('/agent-workspaces/default')) return route.fulfill({ json: { id: 'probe-workspace', display_name: '测试工作区' } });
    if (path.endsWith('/mcp-readiness')) {
      const id = path.split('/').at(-2)!;
      calls.push(id);
      return route.fulfill({ json: await probe(id) }).catch(() => {});
    }
    if (path.endsWith('/mcp-source')) return route.fulfill({ json: { content: '{}', mcp_scripts: [] } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /MCP/ }).click();
  await expect(page.locator('.capability-card')).toHaveCount(3);
  return calls;
}

const ready = (tools = ['lookup', 'status']) => ({ state: 'READY', error_kind: null, checked_at: '2026-10-29T09:00:00Z', tools });

test('toolbar tests selected MCPs sequentially and preserves each tool catalog', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const calls = await setup(page, async id => {
    if (id === 'mcp-0') { await gate; return ready(); }
    return { state: 'UNAVAILABLE', error_kind: 'timeout', checked_at: '2026-10-29T09:00:00Z', tools: [] };
  });
  await page.getByLabel('选择能力 localCode').check();
  await page.getByLabel('选择能力 codeGraph').check();
  await page.getByRole('button', { name: '测试连接 (2)', exact: true }).click();
  await expect.poll(() => calls).toEqual(['mcp-0']);
  const dialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(dialog).toContainText('检测中');
  release();
  await expect(dialog).toContainText('完成 · 1/2 已连接');
  await expect(dialog).toContainText('lookup');
  await expect(dialog).toContainText('连接超时');
  expect(calls).toEqual(['mcp-0', 'mcp-1']);
  await dialog.getByRole('button', { name: '关闭连接测试' }).click();
  await expect(page.getByLabel('选择能力 localCode')).toBeChecked();
  await page.getByRole('button', { name: '查看 MCP localCode 的工具', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP localCode 详情' });
  await expect(details).toContainText('lookup');
  await expect(details).toContainText('status');
  await page.screenshot({ path: '../../.tmp/ops75/mcp-tools.png' });
});

test('testing all includes protected builtins without enabling deletion or editing', async ({ page }) => {
  const calls = await setup(page, async id => ready(id === 'mcp-2' ? ['read_referenced_conversation'] : []));
  await expect(page.getByLabel('选择能力 flowweave-reference-history')).toBeEnabled();
  await page.getByRole('button', { name: '测试全部 MCP', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(dialog).toContainText('完成 · 3/3 已连接');
  await expect(dialog).toContainText('read_referenced_conversation');
  await expect(dialog).toContainText('连接成功，未提供工具');
  expect(calls).toEqual(['mcp-0', 'mcp-1', 'mcp-2']);
  await page.screenshot({ path: '../../.tmp/ops75/mcp-tests.png' });
  await dialog.getByRole('button', { name: '关闭连接测试' }).click();
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history 的工具', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP flowweave-reference-history 详情' });
  await expect(details).toContainText('支持分页、搜索');
  await expect(details.getByRole('button', { name: /基于 v2 编辑/ })).toHaveCount(0);
});

test('closing a connection test cancels the remaining batch', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const calls = await setup(page, async () => { await gate; return ready(); });
  await page.getByRole('button', { name: '测试全部 MCP', exact: true }).click();
  await expect.poll(() => calls.length).toBe(1);
  await page.getByRole('button', { name: '关闭连接测试' }).click();
  release();
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  expect(calls).toEqual(['mcp-0']);
  await expect(page.getByRole('dialog', { name: '测试 MCP 连接', exact: true })).toHaveCount(0);
});

test('returning to a completed test preserves results without issuing more probes', async ({ page }) => {
  const calls = await setup(page, async () => ready());
  await page.getByRole('button', { name: '测试全部 MCP', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(dialog).toContainText('完成 · 3/3 已连接');
  await page.getByRole('button', { name: '流程编排', exact: true }).dispatchEvent('click');
  await page.getByRole('button', { name: '能力仓库', exact: true }).dispatchEvent('click');
  await expect(dialog).toContainText('完成 · 3/3 已连接');
  expect(calls).toHaveLength(3);
});


test('many tools scroll inside the test on a short viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 650 });
  await setup(page, async () => ready(Array.from({ length: 75 }, (_, index) => `lookup_tool_${index}`)));
  await page.getByLabel('选择能力 localCode').check();
  await page.getByRole('button', { name: '测试连接 (1)', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(dialog).toContainText('完成 · 1/1 已连接');
  await expect(dialog.getByRole('button', { name: '关闭连接测试' })).toBeInViewport();
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeInViewport();
  expect(await dialog.locator('.mcp-connection-scroll').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1)).toBe(true);
  await page.screenshot({ path: '../../.tmp/ops75/mcp-tests-1024.png' });
});


test('builtins can be selected alone, in mixed selections and by select all, while deletion stays blocked', async ({ page }) => {
  const calls = await setup(page, async () => ready());
  const deletes: string[] = [];
  page.on('request', request => { if (request.method() === 'DELETE') deletes.push(request.url()); });
  const builtin = page.getByLabel('选择能力 flowweave-reference-history');
  await builtin.check();
  await expect(page.getByRole('button', { name: '批量删除 (1)', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '测试连接 (1)', exact: true }).click();
  const testDialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(testDialog).toContainText('完成 · 1/1 已连接');
  expect(calls).toEqual(['mcp-2']);
  await testDialog.getByRole('button', { name: '关闭连接测试' }).click();
  await page.getByLabel('选择能力 localCode').check();
  await expect(page.getByRole('button', { name: '批量删除 (2)', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '测试连接 (2)', exact: true }).click();
  await expect(testDialog).toContainText('完成 · 2/2 已连接');
  expect(calls).toEqual(['mcp-2', 'mcp-2', 'mcp-0']);
  await testDialog.getByRole('button', { name: '关闭连接测试' }).click();
  await builtin.uncheck();
  await expect(page.getByRole('button', { name: '批量删除 (1)', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '全选当前模块', exact: true }).click();
  await expect(builtin).toBeChecked();
  await expect(page.getByRole('button', { name: '批量删除 (3)', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '测试连接 (3)', exact: true })).toBeEnabled();
  expect(deletes).toEqual([]);
});

test('builtin detail tests inline, prevents duplicate probes and updates the table result', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const calls = await setup(page, async () => { await gate; return ready(['read_referenced_conversation']); });
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP flowweave-reference-history 详情' });
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect.poll(() => calls).toEqual(['mcp-2']);
  await expect(details.getByRole('button', { name: '测试中…', exact: true })).toBeDisabled();
  await expect(details.getByRole('status')).toContainText('检测中');
  await expect(details.getByRole('button', { name: /基于 v2 编辑/ })).toHaveCount(0);
  await expect(page.getByRole('dialog', { name: '测试 MCP 连接', exact: true })).toHaveCount(0);
  release();
  await expect(details.getByRole('status')).toHaveText('已连接');
  await expect(details).toContainText('read_referenced_conversation');
  await expect(details.getByRole('button', { name: '测试连接', exact: true })).toBeEnabled();
  await page.screenshot({ path: '../../.tmp/ops75/builtin-detail.png' });
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: '查看 MCP flowweave-reference-history', exact: true })).toBeFocused();
  await expect(page.locator('.capability-card').filter({ hasText: 'flowweave-reference-history' })).toContainText('1 个工具');
});

test('detail tests selected historical version and replaces stale tool catalogs on failure and retry', async ({ page }) => {
  let fail = false;
  const calls = await setup(page, async id => fail ? { state: 'UNAVAILABLE', error_kind: 'timeout', checked_at: '2026-10-29T09:00:00Z', tools: [] } : ready([id === 'builtin-old' ? 'old_tool' : 'current_tool']), [
    { ...items[2], id: 'builtin-old', revision_number: 1, is_latest: false },
  ]);
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP flowweave-reference-history 详情' });
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details).toContainText('current_tool');
  await details.getByRole('complementary').getByRole('button', { name: /v1/ }).click();
  await expect(details).not.toContainText('current_tool');
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details).toContainText('old_tool');
  expect(calls).toEqual(['mcp-2', 'builtin-old']);
  fail = true;
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details.getByRole('status')).toContainText('连接超时');
  await expect(details).not.toContainText('old_tool');
  fail = false;
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details).toContainText('old_tool');
  await details.getByRole('complementary').getByRole('button', { name: /v2/ }).click();
  await expect(details).toContainText('current_tool');
  await expect(details).not.toContainText('old_tool');
});

test('version changes and closing details cancel in-flight tests without publishing late tools', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const calls = await setup(page, async id => {
    if (id === 'mcp-2') await gate;
    return ready([id === 'mcp-2' ? 'late_tool' : 'history_tool']);
  }, [{ ...items[2], id: 'builtin-old', revision_number: 1, is_latest: false }]);
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP flowweave-reference-history 详情' });
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect.poll(() => calls).toEqual(['mcp-2']);
  await details.getByRole('complementary').getByRole('button', { name: /v1/ }).click();
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details).toContainText('history_tool');
  release();
  await details.getByRole('complementary').getByRole('button', { name: /v2/ }).click();
  await expect(details.getByRole('status')).toHaveText('未测试');
  await expect(details).not.toContainText('late_tool');
  const aborted = page.waitForEvent('requestfailed', { predicate: request => request.url().includes('/mcp-2/mcp-readiness') });
  let finishClosed!: () => void;
  const closedGate = new Promise<void>(resolve => { finishClosed = resolve; });
  await page.route('**/mcp-2/mcp-readiness', async route => { await closedGate; await route.fulfill({ json: ready(['closed_tool']) }).catch(() => {}); });
  const started = page.waitForRequest(request => request.url().includes('/mcp-2/mcp-readiness'));
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await started;
  await expect(details.getByRole('button', { name: '测试中…', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  await aborted;
  finishClosed();
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history', exact: true }).click();
  await details.getByRole('button', { name: '工具能力', exact: true }).click();
  await expect(details.getByRole('status')).toHaveText('未测试');
  await expect(details).not.toContainText('closed_tool');
});

test('detail test action stays visible with long tool lists on short and mobile viewports', async ({ page }) => {
  await setup(page, async () => ready(Array.from({ length: 75 }, (_, index) => `tool_${index}`)));
  await page.getByRole('button', { name: '查看 MCP localCode', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP localCode 详情' });
  await details.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(details).toContainText('tool_74');
  for (const width of [1024, 390]) {
    await page.setViewportSize({ width, height: 650 });
    await expect(details.getByRole('button', { name: '测试连接', exact: true })).toBeInViewport();
    await expect(details.getByRole('button', { name: '关闭', exact: true })).toBeInViewport();
    expect(await details.locator('.mcp-details-content').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
    expect(await details.evaluate(element => element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1)).toBe(true);
    await page.screenshot({ path: `../../.tmp/ops75/detail-${width}.png` });
  }
});
