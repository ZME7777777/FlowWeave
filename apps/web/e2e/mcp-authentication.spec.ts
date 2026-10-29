import { expect, test, type Page } from '@playwright/test';

const mcp = { id: 'version-2', lineage_id: 'mcp-lineage', capability_key: 'kdocs', capability_type: 'MCP', revision_number: 2, is_latest: true, is_builtin: false, description: '云文档', filename: 'mcp.json', byte_size: 180, created_at: '2026-10-30T08:00:00Z', reference_count: 0, document: { transport: 'streamable-http', url: 'https://mcp.example.test/mcp' }, dependencies: {}, dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null };
const credentials = [
  { id: 'my-token', name: '文档账号', target_host: 'mcp.example.test', target_path: '/mcp', auth_type: 'TOKEN', include_subdomains: false },
  { id: 'wrong-host', name: '其他网站', target_host: 'other.example.test', target_path: '/', auth_type: 'TOKEN', include_subdomains: false },
  { id: 'password', name: '密码认证', target_host: 'mcp.example.test', target_path: '/', auth_type: 'PASSWORD', include_subdomains: false },
];
async function setup(page: Page) {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  let auth = { capability_version_id: mcp.id, strategy: 'none', credential_id: null as string | null, credential_name: null as string | null, row_version: 0, editable: true, valid: true };
  await page.route('**/api/v1/**', async route => {
    const req = route.request(); const path = new URL(req.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ json: { id: 'user-a', username: 'user-a', role: 'USER', is_super_admin: false } });
    if (req.method() !== 'GET') writes.push({ path, body: req.postDataJSON() ?? {} });
    if (path.endsWith('/mcp-authentication')) {
      if (req.method() === 'PUT') { const body = req.postDataJSON(); auth = { ...auth, ...body, credential_name: body.credential_id ? '文档账号' : null, row_version: auth.row_version + 1 }; }
      return route.fulfill({ json: auth });
    }
    if (path.endsWith('/website-credentials')) return route.fulfill({ json: credentials });
    if (path.endsWith('/capability-directory')) return route.fulfill({ json: { items: [{ id: mcp.lineage_id, latest: mcp, versions: [mcp] }], total: 1, page: 1, page_size: 20, counts: { MCP: 1 }, collection_total: 0 } });
    if (path.endsWith('/capabilities')) return route.fulfill({ json: [mcp] });
    if (path.endsWith('/mcp-source')) return route.fulfill({ json: { id: mcp.id, capability_key: 'kdocs', content: JSON.stringify({ mcpServers: { kdocs: mcp.document } }, null, 2), mcp_scripts: [] } });
    if (path.endsWith('/agent-workspaces/default')) return route.fulfill({ json: { id: 'workspace', display_name: '工作区' } });
    if (path.endsWith('/mcp-readiness')) return route.fulfill({ json: { state: 'READY', error_kind: null, checked_at: '2026-10-30T08:00:00Z', tools: ['search_files'], ...(req.postDataJSON()?.read_only_tool_call ? { read_only_tool_call: { name: 'search_files', is_error: false, business_code: 0 } } : {}) } });
    if (path.endsWith('/capability-imports/validate')) return route.fulfill({ json: { import_token: 'import-ref', preview: { capabilities: [{ capability_key: 'docs' }] } } });
    if (path.endsWith('/capability-imports')) return route.fulfill({ status: 201, json: { capabilities: [mcp] } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /MCP/ }).click();
  return writes;
}

for (const width of [1440, 390]) test(`MCP authentication stays compact, scoped and persistent across tabs at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 850 });
  const writes = await setup(page);
  await page.locator('.capability-card').getByText('kdocs', { exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'MCP kdocs 详情' });
  await dialog.getByRole('button', { name: '认证', exact: true }).click();
  const method = dialog.getByRole('button', { name: 'MCP 认证方式' });
  await method.focus(); await page.keyboard.press('ArrowDown');
  await page.getByRole('option', { name: 'Bearer Token', exact: true }).click();
  await dialog.getByRole('button', { name: 'MCP 关联认证' }).click();
  await expect(page.getByRole('option', { name: /其他网站|密码认证/ })).toHaveCount(0);
  await page.getByRole('option', { name: /文档账号/ }).click();
  await dialog.getByRole('button', { name: '配置', exact: true }).click();
  await dialog.getByRole('button', { name: '认证', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'MCP 关联认证' })).toContainText('文档账号');
  await method.click(); await page.keyboard.press('Escape');
  await expect(dialog).toBeVisible(); await expect(method).toBeFocused();
  await method.click(); await dialog.getByRole('heading', { name: 'kdocs', exact: true }).click();
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await dialog.getByRole('button', { name: '保存认证', exact: true }).click();
  await expect(dialog).toContainText('已保存，可重新测试连接');
  expect(writes.filter(item => item.path.endsWith('/mcp-authentication'))).toEqual([{ path: '/api/v1/capabilities/version-2/mcp-authentication', body: { strategy: 'bearer', credential_id: 'my-token', expected_row_version: 0 } }]);
  expect(writes.some(item => item.path.endsWith('/mcp-source'))).toBe(false);
  expect(await dialog.locator('select').count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1)).toBe(true);
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1 && element.getBoundingClientRect().right <= innerWidth)).toBe(true);
  await page.screenshot({ path: `../../.tmp/conversation-analysis/auth-${width}.png` });
  await dialog.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(dialog).toContainText('search_files');
});

test('new MCP commits the credential reference separately from shared JSON', async ({ page }) => {
  const writes = await setup(page);
  await page.getByRole('button', { name: '新建 MCP', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '新建 MCP', exact: true });
  await dialog.getByLabel('Server 名称', { exact: true }).fill('docs');
  await dialog.getByLabel('Server URL', { exact: true }).fill('https://mcp.example.test/mcp');
  await dialog.getByRole('button', { name: 'MCP 认证方式' }).click();
  await page.getByRole('option', { name: 'Bearer Token' }).click();
  await expect(dialog.getByRole('button', { name: '校验并保存' })).toBeDisabled();
  await dialog.getByRole('button', { name: 'MCP 关联认证' }).click();
  await page.getByRole('option', { name: /文档账号/ }).click();
  await dialog.getByRole('button', { name: '校验并保存' }).click();
  await expect(dialog).toHaveCount(0);
  const validation = writes.find(item => item.path.endsWith('/validate'))!.body;
  const source = Buffer.from(String(validation.content_base64), 'base64').toString();
  expect(source).not.toContain('my-token');
  expect(writes.find(item => item.path.endsWith('/capability-imports'))!.body).toEqual({ import_token: 'import-ref', mcp_authentication: { strategy: 'bearer', credential_id: 'my-token', expected_row_version: 0 } });
});

test('detail test invokes only the explicitly supplied read-only tool and reports business code', async ({ page }) => {
  const writes = await setup(page);
  await page.locator('.capability-card').getByText('kdocs', { exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'MCP kdocs 详情' });
  await dialog.getByRole('button', { name: '工具能力', exact: true }).click();
  await dialog.getByText('附加只读工具验证（可选）').click();
  await dialog.getByLabel('只读工具名称').fill('search_files');
  await dialog.getByLabel('只读工具参数').fill('{"keyword":"测试","type":"all"}');
  await dialog.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(dialog).toContainText('业务码 0');
  expect(writes.find(item => item.path.endsWith('/mcp-readiness'))!.body).toEqual({ read_only_tool_call: { name: 'search_files', arguments: { keyword: '测试', type: 'all' } } });
});
