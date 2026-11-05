import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-28T02:00:00Z';
const admin = { id: 'settings-admin', username: 'manager', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
}
function credential(index: number) {
  return { id: `credential-${index}`, name: `站点认证 ${index}`, target_host: `service-${index}.example.test`, target_path: '/admin', include_subdomains: false, auth_type: 'USERNAME_PASSWORD', has_username: true, has_secret: true, secret_hint: '5678', row_version: 1, environment_names: {}, created_at: now, updated_at: now };
}
function provider(index: number) {
  return { id: `provider-${index}`, name: `模型服务 ${index}`, auth_type: 'API_KEY', base_url: 'https://models.example.test/v1', protocols: ['CHAT_COMPLETIONS'], has_api_key: true, api_key_hint: '••••key', oauth_connected: false, connection_state: 'CONNECTED', reference_node_count: 2, available_for_nodes: true, available_for_prompt_gates: true, row_version: 1, models: [{ id: `model-${index}`, model_name: `model-${index}`, enabled: true, is_default: true }], created_at: now, updated_at: now };
}
function environment(index: number) {
  return { id: `environment-${index}`, name: `终端环境 ${index}`, description: `开发与验收环境 ${index}`, row_version: 1, active_sessions: [], versions: Array.from({ length: 12 }, (_, revision) => ({ id: `version-${index}-${revision}`, environment_id: `environment-${index}`, version_no: 12 - revision, state: 'READY', image_reference: 'test/runtime:v1', image_digest: 'sha256:' + 'a'.repeat(64), description: `环境版本 ${12 - revision}`, runtime_capabilities: ['browser'], manifest: { commands: { python: '3.12' } }, reference_count: revision === 0 ? 1 : 0, run_reference_count: revision === 0 ? 1 : 0, created_at: now })), created_at: now, updated_at: now };
}
async function openSettings(page: Page, title: string) {
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: title, exact: true }).click();
}
async function base(page: Page, handler?: (route: Route, path: string) => Promise<boolean>) {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (handler && await handler(route, path)) return;
    if (path.endsWith('/auth/me') || path.endsWith('/auth/login')) return json(route, admin);
    if (path.endsWith('/terminal-environments')) return json(route, Array.from({ length: 25 }, (_, index) => environment(index + 1)));
    if (path.endsWith('/website-credentials')) return json(route, Array.from({ length: 25 }, (_, index) => credential(index + 1)));
    if (path.endsWith('/model-providers')) return json(route, Array.from({ length: 25 }, (_, index) => provider(index + 1)));
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
}

test('all three management pages share compact lists, selection, search, and fixed button pagination', async ({ page }) => {
  await base(page);
  for (const [title, name] of [['终端环境', '终端环境'], ['认证管理', '站点认证'], ['供应商配置', '模型服务']]) {
    await openSettings(page, title);
    const list = page.getByRole('complementary', { name: `${title}列表` });
    const detail = page.getByRole('main', { name: `${title}详情` });
    await expect(list.locator('.settings-resource-row')).toHaveCount(20);
    await expect(detail.getByRole('heading', { name: `${name} 1`, exact: true })).toBeVisible();
    const titleButton = list.getByRole('button', { name: `查看 ${name} 2`, exact: true });
    if (title !== '终端环境') {
      const resource = titleButton.locator('xpath=ancestor::div[contains(@class, "settings-resource-row")]');
      await resource.click({ position: { x: 2, y: 2 } });
      await expect(resource.getByRole('checkbox')).toBeChecked();
      await expect(detail.getByRole('heading', { name: `${name} 1`, exact: true })).toBeVisible();
      await titleButton.click();
      await expect(resource.getByRole('checkbox')).toBeChecked();
    } else await titleButton.click();
    await expect(detail.getByRole('heading', { name: `${name} 2`, exact: true })).toBeVisible();
    await expect(titleButton).toHaveAttribute('aria-pressed', 'true');
    for (const width of [1920, 1440, 1024]) {
      await page.setViewportSize({ width, height: 600 });
      const bounds = await list.evaluate(element => {
        const host = element.closest('.settings-management-page')!;
        const right = host.querySelector('.settings-management-detail')!;
        const scroll = element.querySelector('.settings-management-list-scroll')!;
        const header = element.querySelector('header')!;
        return { left: element.getBoundingClientRect().right, right: right.getBoundingClientRect().left,
          bottom: host.getBoundingClientRect().bottom, pageHeight: document.documentElement.scrollHeight,
          viewport: innerHeight, scroll: scroll.scrollHeight, client: scroll.clientHeight,
          heading: header.getBoundingClientRect().height };
      });
      expect(bounds.left).toBeLessThanOrEqual(bounds.right + 1);
      expect(bounds.bottom).toBeLessThanOrEqual(601);
      expect(bounds.pageHeight).toBeLessThanOrEqual(bounds.viewport);
      expect(bounds.heading).toBeLessThanOrEqual(60);
      expect(bounds.scroll).toBeGreaterThan(bounds.client);
      await expect(list.getByRole('button', { name: '下一页', exact: true })).toBeInViewport();
      await list.locator('.settings-management-list-scroll').hover(); await page.mouse.wheel(0, 300);
      await expect.poll(() => list.locator('.settings-management-list-scroll').evaluate(node => node.scrollTop)).toBeGreaterThan(0);
      await list.getByRole('button', { name: '下一页', exact: true }).click();
      await expect(list.locator('.settings-resource-row')).toHaveCount(5);
      await expect(detail.getByRole('heading', { name: `${name} 21`, exact: true })).toBeVisible();
      await list.getByRole('button', { name: '上一页', exact: true }).click();
      await expect(list.locator('.settings-resource-row')).toHaveCount(20);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    await list.getByRole('textbox', { name: `搜索${title}` }).fill(`${name} 25`);
    await expect(list.locator('.settings-resource-row')).toHaveCount(1);
    await expect(detail.getByRole('heading', { name: `${name} 25`, exact: true })).toBeVisible();
    await list.getByRole('textbox', { name: `搜索${title}` }).fill('不存在');
    await expect(list.locator('.settings-resource-row')).toHaveCount(0);
    await list.getByRole('textbox', { name: `搜索${title}` }).fill('');
    await expect(list.locator('.settings-resource-row')).toHaveCount(20);
    await page.screenshot({ path: test.info().outputPath(`${title}.png`) });
  }
});

test('credentials preserve blank secret editing, create, current-page selection, and deletion', async ({ page }) => {
  let credentials = [credential(1), credential(2)];
  const writes: Array<Record<string, unknown>> = [];
  await base(page, async (route, path) => {
    const method = route.request().method();
    if (path.includes('/website-credentials')) {
      if (method === 'PUT') { const write = route.request().postDataJSON(); writes.push(write); credentials[0] = { ...credentials[0], name: write.name }; await json(route, credentials[0]); }
      else if (method === 'POST') { const write = route.request().postDataJSON(); writes.push(write); credentials = [...credentials, { ...credential(3), ...write }]; await json(route, credentials.at(-1), 201); }
      else if (method === 'DELETE') { const ids = route.request().postDataJSON()?.ids ?? [path.split('/').at(-1)]; credentials = credentials.filter(item => !ids.includes(item.id)); await json(route, { deleted_ids: ids }); }
      else await json(route, credentials);
      return true;
    }
    return false;
  });
  await openSettings(page, '认证管理');
  const detail = page.getByRole('main', { name: '认证管理详情' });
  await detail.getByRole('button', { name: '编辑 站点认证 1', exact: true }).click();
  const editor = page.locator('form.credential-editor');
  await expect(editor.getByLabel('用户名', { exact: true })).toHaveValue('');
  await expect(editor.getByLabel('密码', { exact: true })).toHaveValue('');
  await editor.getByLabel('名称', { exact: true }).fill('更新认证');
  await editor.getByRole('button', { name: '保存认证信息' }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ name: '更新认证', username: '', secret: '' });
  await expect(detail.getByRole('heading', { name: '更新认证', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新增认证', exact: true }).click();
  await editor.getByLabel('名称', { exact: true }).fill('Token 认证');
  await editor.getByRole('button', { name: '认证方式', exact: true }).click();
  await editor.getByRole('option', { name: 'Token', exact: true }).click();
  await editor.getByLabel(/^目标地址/).fill('api.example.test');
  await editor.getByLabel(/^目标路径/).fill('/v1');
  await editor.getByLabel(/^Token/).fill('fixture-token');
  await editor.getByRole('button', { name: '保存认证信息' }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({ auth_type: 'TOKEN', target_path: '/v1' });
  await page.getByRole('button', { name: '全选当前页' }).click();
  await page.getByRole('button', { name: '批量删除 (3)' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.settings-resource-row')).toHaveCount(0);
});

test('model detail keeps test feedback and budget bound to the selected service and retains all model settings', async ({ page }) => {
  let providers = [provider(1), provider(2)];
  providers[0].models.push({ id: 'disabled', model_name: 'disabled-model', enabled: false, is_default: false });
  let tested = ''; let budgetReads = 0;
  await base(page, async (route, path) => {
    if (path.endsWith('/test')) { tested = path.includes('provider-1') ? '1' : '2'; await json(route, { connection_state: 'CONNECTED', model_count: 2 }); return true; }
    if (path.endsWith('/usage')) { budgetReads += 1; await json(route, { status: 'AVAILABLE', spend: 4, max_budget: 10, remaining: 6 }); return true; }
    if (path.endsWith('/model-providers') && route.request().method() === 'DELETE') { const ids = route.request().postDataJSON().ids; providers = providers.filter(item => !ids.includes(item.id)); await json(route, { deleted_ids: ids, session_reconfigured: 1, automatic_reconfigured: 0, automatic_needs_model_configuration: 0 }); return true; }
    if (path.endsWith('/model-providers')) { await json(route, providers); return true; }
    return false;
  });
  await openSettings(page, '供应商配置');
  const detail = page.getByRole('main', { name: '供应商配置详情' });
  await expect(detail.getByRole('row').filter({ hasText: 'disabled-model' })).toContainText('已停用');
  await detail.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(detail.getByRole('status')).toContainText('连接成功');
  expect(tested).toBe('1');
  await detail.getByRole('button', { name: '查询用量', exact: true }).click();
  await expect(detail.getByText('剩余 6', { exact: true })).toBeVisible();
  expect(budgetReads).toBe(1);
  await page.getByRole('button', { name: '查看 模型服务 2', exact: true }).click();
  await expect(detail.getByRole('status')).toHaveCount(0);
  await expect(detail.getByRole('button', { name: '查询用量' })).toBeVisible();
  await detail.getByRole('button', { name: '编辑供应商 模型服务 2', exact: true }).click();
  const editor = page.locator('form.model-editor');
  await expect(editor.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(editor.getByRole('button', { name: '拉取模型', exact: true })).toBeVisible();
  await expect(editor.getByRole('button', { name: '手动添加模型', exact: true })).toBeVisible();
  await editor.getByRole('button', { name: '关闭', exact: true }).click();
  await detail.getByRole('button', { name: '删除供应商 模型服务 2', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByRole('status').filter({ hasText: '已删除' })).toContainText('已删除 1 个供应商');
  await expect(detail.getByRole('heading', { name: '模型服务 1', exact: true })).toBeVisible();
});

test('environment version table preserves reference protection and deletes only the chosen unused version', async ({ page }) => {
  const env = environment(1);
  let deleted = '';
  await base(page, async (route, path) => {
    if (route.request().method() === 'DELETE' && path.includes('/versions/')) {
      deleted = path.split('/').at(-1)!; env.versions = env.versions.filter(item => item.id !== deleted);
      await route.fulfill({ status: 204 }); return true;
    }
    if (path.endsWith('/terminal-environments')) { await json(route, [env]); return true; }
    return false;
  });
  await openSettings(page, '终端环境');
  const detail = page.getByRole('main', { name: '终端环境详情' });
  await expect(detail.getByRole('button', { name: '删除版本 v12', exact: true })).toBeDisabled();
  await detail.getByRole('button', { name: '删除版本 v11', exact: true }).click();
  const confirm = page.getByRole('alertdialog');
  await expect(confirm).toContainText('无法撤销');
  await confirm.getByRole('button', { name: '删除版本', exact: true }).click();
  await expect.poll(() => deleted).toBe('version-1-1');
  await expect(detail.getByRole('button', { name: '删除版本 v11', exact: true })).toHaveCount(0);
  await expect(detail.getByRole('button', { name: '从 v12 创建草稿', exact: true })).toBeVisible();
});

test('one provider derives each model protocol automatically and keeps manual overrides', async ({ page }) => {
  let saved: Record<string, unknown> | undefined;
  await base(page, async (route, path) => {
    if (path.endsWith('/discover-models')) { await json(route, { models: ['claude-sonnet-5-5', 'gpt-5.5', 'deepseek-chat'] }); return true; }
    if (path.endsWith('/model-providers') && route.request().method() === 'POST') { saved = route.request().postDataJSON(); await json(route, { ...provider(99), ...saved }, 201); return true; }
    return false;
  });
  await openSettings(page, '供应商配置');
  await page.getByRole('button', { name: '新增供应商', exact: true }).click();
  const editor = page.locator('form.model-editor');
  await expect(editor.getByRole('button', { name: '调用协议', exact: true })).toHaveCount(0);
  await editor.getByLabel('服务名称', { exact: true }).fill('新供应商');
  await editor.getByLabel('Base URL', { exact: true }).fill('https://models.example.test');
  await editor.getByLabel('API Key', { exact: true }).fill('fixture-key');
  await editor.getByRole('button', { name: '拉取模型', exact: true }).click();
  for (const name of ['claude-sonnet-5-5', 'gpt-5.5', 'deepseek-chat']) await editor.getByRole('button', { name, exact: true }).click();
  await expect(editor.getByRole('button', { name: '模型 claude-sonnet-5-5 协议' })).toContainText('Anthropic Messages');
  await expect(editor.getByRole('button', { name: '模型 gpt-5.5 协议' })).toContainText('Responses');
  await expect(editor.getByRole('button', { name: '模型 deepseek-chat 协议' })).toContainText('Chat Completions');
  await editor.getByRole('button', { name: '模型 gpt-5.5 协议' }).click();
  await editor.getByRole('option', { name: 'Chat Completions', exact: true }).click();
  await editor.getByRole('radio').nth(1).check();
  await editor.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect.poll(() => saved?.models?.length).toBe(3);
  expect(saved?.api_protocol).toBeUndefined();
  expect(saved?.models).toEqual([
    { model_name: 'claude-sonnet-5-5', enabled: true, is_default: false, protocol: null },
    { model_name: 'gpt-5.5', enabled: true, is_default: true, protocol: 'CHAT_COMPLETIONS' },
    { model_name: 'deepseek-chat', enabled: true, is_default: false, protocol: null },
  ]);
});

test('Codex detail retains device login, model refresh, and disconnect', async ({ page }) => {
  const codex = { ...provider(1), auth_type: 'CODEX_OAUTH', oauth_connected: false, oauth_account_email: '', models: [] as ReturnType<typeof provider>['models'] };
  let disconnects = 0; let discoveries = 0;
  await page.addInitScript(() => { window.open = () => null; });
  await base(page, async (route, path) => {
    if (path.endsWith('/model-providers')) { await json(route, [codex]); return true; }
    if (path.endsWith('/device/start')) { await json(route, { verification_url: 'https://login.example.test/device', user_code: 'FIXTURE-CODE', expires_at: '2027-01-01T00:00:00Z', interval: 5 }); return true; }
    if (path.endsWith('/device/poll')) { codex.oauth_connected = true; codex.oauth_account_email = 'fixture@example.test'; codex.models = [{ id: 'native-model', model_name: 'codex-fixture', enabled: true, is_default: true }]; await json(route, { connected: true, model_count: 1 }); return true; }
    if (path.endsWith('/discover-models')) { discoveries += 1; await json(route, { models: ['codex-fixture'], provider: codex }); return true; }
    if (path.endsWith('/oauth') && route.request().method() === 'DELETE') { disconnects += 1; codex.oauth_connected = false; await json(route, codex); return true; }
    return false;
  });
  await openSettings(page, '供应商配置');
  const detail = page.getByRole('main', { name: '供应商配置详情' });
  await detail.getByRole('button', { name: '登录 Codex', exact: true }).click();
  await expect(page.getByText('FIXTURE-CODE', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '我已完成授权', exact: true }).click();
  await expect(detail.getByText('fixture@example.test', { exact: true })).toBeVisible();
  await detail.getByRole('button', { name: '编辑供应商 模型服务 1', exact: true }).click();
  const editor = page.locator('form.model-editor');
  await expect.poll(() => discoveries).toBeGreaterThanOrEqual(1);
  await expect(editor.getByText('codex-fixture', { exact: true })).toBeVisible();
  await expect(editor.getByRole('button', { name: '刷新模型', exact: true })).toBeEnabled();
  const beforeRefresh = discoveries;
  await editor.getByRole('button', { name: '刷新模型', exact: true }).click();
  await expect.poll(() => discoveries).toBeGreaterThan(beforeRefresh);
  await editor.getByRole('button', { name: '关闭', exact: true }).click();
  await detail.getByRole('button', { name: '断开 Codex', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '断开连接', exact: true }).click();
  await expect.poll(() => disconnects).toBe(1);
  await expect(detail.getByRole('button', { name: '登录 Codex', exact: true })).toBeVisible();
});

test('bulk selection keeps the original scope across pages for credentials and model services', async ({ page }) => {
  let credentials = Array.from({ length: 25 }, (_, index) => credential(index + 1));
  let providers = Array.from({ length: 25 }, (_, index) => provider(index + 1));
  const deletions: number[] = [];
  await base(page, async (route, path) => {
    if (!path.endsWith('/website-credentials') && !path.endsWith('/model-providers')) return false;
    const isCredentials = path.endsWith('/website-credentials');
    if (route.request().method() === 'DELETE') {
      const ids = route.request().postDataJSON().ids as string[]; deletions.push(ids.length);
      if (isCredentials) credentials = credentials.filter(item => !ids.includes(item.id));
      else providers = providers.filter(item => !ids.includes(item.id));
      await json(route, { deleted_ids: ids, session_reconfigured: 0, automatic_reconfigured: 0, automatic_needs_model_configuration: 0 });
    } else await json(route, isCredentials ? credentials : providers);
    return true;
  });
  await openSettings(page, '认证管理');
  const list = page.getByRole('complementary', { name: '认证管理列表' });
  await list.getByRole('button', { name: '全选当前页' }).click();
  await expect(list.getByRole('button', { name: '批量删除 (20)' })).toBeEnabled();
  await list.getByRole('button', { name: '下一页' }).click();
  await list.getByRole('button', { name: '全选当前页' }).click();
  await list.getByRole('button', { name: '批量删除 (25)' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除' }).click();
  await expect(list.locator('.settings-resource-row')).toHaveCount(0);
  await openSettings(page, '供应商配置');
  const models = page.getByRole('complementary', { name: '供应商配置列表' });
  await models.getByRole('button', { name: '全选', exact: true }).click();
  await models.getByRole('button', { name: '下一页' }).click();
  await expect(models.getByRole('checkbox').first()).toBeChecked();
  await models.getByRole('button', { name: '批量删除 (25)' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除' }).click();
  await expect(models.locator('.settings-resource-row')).toHaveCount(0);
  expect(deletions).toEqual([25, 25]);
});

test('custom model budget query previews unsaved settings and preserves templates when editing', async ({ page }) => {
  let configured = { ...provider(1), usage_query: null as import('../src/types').ProviderUsageQuery | null };
  const previews: Array<Record<string, unknown>> = [];
  const writes: Array<Record<string, unknown>> = [];
  const metrics = [
    { label: '剩余', value: 200, currency: true, percentage: false },
    { label: '使用率', value: 60, currency: false, percentage: true },
    { label: '缺失', value: null, currency: false, percentage: false },
  ];
  await base(page, async (route, path) => {
    if (path.endsWith('/preview-usage')) { previews.push(route.request().postDataJSON()); await json(route, { status: 'AVAILABLE', metrics }); return true; }
    if (path.endsWith('/usage')) { await json(route, { status: 'AVAILABLE', metrics }); return true; }
    if (path.endsWith('/provider-1') && route.request().method() === 'PUT') {
      const body = route.request().postDataJSON(); writes.push(body);
      configured = { ...configured, ...body, row_version: configured.row_version + 1 };
      await json(route, configured); return true;
    }
    if (path.endsWith('/model-providers')) { await json(route, [configured]); return true; }
    return false;
  });
  await openSettings(page, '供应商配置');
  const detail = page.getByRole('main', { name: '供应商配置详情' });
  await detail.getByRole('button', { name: '编辑供应商 模型服务 1', exact: true }).click();
  const editor = page.locator('form.model-editor');
  await editor.getByRole('checkbox', { name: '自定义余额查询', exact: true }).check();
  await editor.getByRole('group', { name: '查询请求方法' }).getByRole('button', { name: 'POST', exact: true }).click();
  const url = 'https://budget.example.test/key/info?key={key}';
  const expression = '剩余: $info.max_budget - info.spend; 使用率: info.spend / info.max_budget %; 缺失: data.missing';
  await editor.getByLabel('余额查询地址', { exact: true }).fill(url);
  await editor.getByLabel('余额字段', { exact: true }).fill(expression);
  await editor.getByText('请求头与数组汇总', { exact: true }).click();
  await editor.getByRole('textbox', { name: '查询请求头（JSON）', exact: true }).fill('{"X-Api-Key":"{key}"}');
  await editor.getByRole('button', { name: '查询余额', exact: true }).click();
  await expect(editor.getByText('$200', { exact: true })).toBeVisible();
  await expect(editor.getByRole('progressbar', { name: '使用率' })).toHaveAttribute('value', '60');
  await expect(editor.getByText('—', { exact: true })).toBeVisible();
  expect(previews[0]).toMatchObject({ provider_id: 'provider-1', api_key: '', usage_query: { method: 'POST', url, expression, headers: { 'X-Api-Key': '{key}' } } });
  expect(writes).toHaveLength(0);
  await page.screenshot({ path: test.info().outputPath('custom-query-editor.png') });
  await editor.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(editor).toHaveCount(0);
  expect(writes[0].usage_query).toEqual((previews[0] as { usage_query: unknown }).usage_query);
  await detail.getByRole('button', { name: '查询余额', exact: true }).click();
  await expect(detail.getByText('$200', { exact: true })).toBeVisible();
  await detail.getByRole('button', { name: '编辑供应商 模型服务 1', exact: true }).click();
  await expect(editor.getByLabel('余额查询地址', { exact: true })).toHaveValue(url);
  await expect(editor.getByLabel('API Key', { exact: true })).toHaveValue('');
  await editor.getByText('请求头与数组汇总', { exact: true }).click();
  await expect(editor.getByRole('textbox', { name: '查询请求头（JSON）', exact: true })).toHaveValue(JSON.stringify({ 'X-Api-Key': '{key}' }, null, 2));
  await editor.getByRole('textbox', { name: '查询请求头（JSON）', exact: true }).fill('{invalid');
  await editor.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(editor.getByText('查询请求头必须是 JSON 对象', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
  await editor.getByRole('checkbox', { name: '自定义余额查询', exact: true }).uncheck();
  await editor.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(editor).toHaveCount(0);
  expect(writes[1].usage_query).toBeNull();
  await expect(detail.getByText('$200', { exact: true })).toHaveCount(0);
});

test('editing a query cancels an outdated balance preview and supports outside closing', async ({ page }) => {
  let releasePreview: (() => void) | undefined;
  let previewStarted = false;
  await base(page, async (route, path) => {
    if (path.endsWith('/model-providers')) { await json(route, [provider(1)]); return true; }
    if (path.endsWith('/preview-usage')) {
      previewStarted = true;
      await new Promise<void>(resolve => { releasePreview = resolve; });
      await json(route, { status: 'AVAILABLE', metrics: [{ label: '旧余额', value: 999, currency: true, percentage: false }] }).catch(() => undefined);
      return true;
    }
    return false;
  });
  await openSettings(page, '供应商配置');
  await page.getByRole('button', { name: '编辑供应商 模型服务 1', exact: true }).click();
  const editor = page.locator('form.model-editor');
  await editor.getByRole('checkbox', { name: '自定义余额查询', exact: true }).check();
  await editor.getByRole('button', { name: '查询余额', exact: true }).click();
  await expect.poll(() => previewStarted).toBe(true);
  await editor.getByLabel('余额字段', { exact: true }).fill('data.balance');
  releasePreview?.();
  await expect(editor.getByRole('button', { name: '查询余额', exact: true })).toBeEnabled();
  await expect(editor.getByText('$999', { exact: true })).toHaveCount(0);
  await page.locator('.modal-backdrop').click({ position: { x: 2, y: 2 } });
  await expect(editor).toHaveCount(0);
});
