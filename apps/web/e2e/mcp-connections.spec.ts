import { expect, test, type Page } from '@playwright/test';

const items = ['localCode', 'codeGraph', 'flowweave-reference-history'].map((name, index) => ({
  id: `mcp-${index}`, lineage_id: `lineage-${index}`, capability_key: name, capability_type: 'MCP',
  revision_number: index === 2 ? 2 : 1, is_latest: true, is_builtin: index === 2,
  description: 'MCP 连接测试', filename: 'mcp.json', byte_size: 200, created_at: '2026-10-29T09:00:00Z',
  reference_count: 0, document: { transport: 'streamable-http', url: 'https://mcp.example.test' },
  dependencies: {}, dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null,
}));

async function setup(page: Page, probe: (id: string) => Promise<unknown>) {
  const calls: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ json: { id: 'mcp-user', username: 'mcp-user', role: 'SUPER_ADMIN', is_super_admin: true } });
    if (path.endsWith('/capability-directory')) return route.fulfill({ json: { items: items.map(item => ({ id: item.lineage_id, latest: item, versions: [item] })), total: items.length, page: 1, page_size: 20, counts: { MCP: 3 }, collection_total: 0 } });
    if (path.endsWith('/capabilities')) return route.fulfill({ json: items });
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
  await page.screenshot({ path: '../../.tmp/ops73/mcp-tools.png' });
});

test('testing all includes protected builtins without enabling deletion or editing', async ({ page }) => {
  const calls = await setup(page, async id => ready(id === 'mcp-2' ? ['read_referenced_conversation'] : []));
  await expect(page.getByLabel('选择能力 flowweave-reference-history')).toBeDisabled();
  await page.getByRole('button', { name: '测试全部 MCP', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '测试 MCP 连接', exact: true });
  await expect(dialog).toContainText('完成 · 3/3 已连接');
  await expect(dialog).toContainText('read_referenced_conversation');
  await expect(dialog).toContainText('连接成功，未提供工具');
  expect(calls).toEqual(['mcp-0', 'mcp-1', 'mcp-2']);
  await page.screenshot({ path: '../../.tmp/ops73/mcp-tests.png' });
  await dialog.getByRole('button', { name: '关闭连接测试' }).click();
  await page.getByRole('button', { name: '查看 MCP flowweave-reference-history 的工具', exact: true }).click();
  const details = page.getByRole('dialog', { name: 'MCP flowweave-reference-history 详情' });
  await expect(details).toContainText('支持分页、搜索');
  await expect(details.getByRole('button', { name: /基于 v2 编辑/ })).toBeDisabled();
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
  await page.screenshot({ path: '../../.tmp/ops73/mcp-tests-1024.png' });
});
