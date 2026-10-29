import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-29T01:00:00Z';
function version(number: number) {
  return { id: `version-${number}`, environment_id: 'environment-1', version_no: number, parent_version_id: number > 1 ? `version-${number - 1}` : null,
    state: 'READY', image_reference: `test/runtime:v${number}`, image_digest: 'sha256:' + String(number).padStart(64, '0'), base_image_reference: 'test/base:1', base_image_digest: 'sha256:' + 'a'.repeat(64),
    description: `版本 ${number} 说明`, runtime_capabilities: ['browser'], manifest: { architecture: 'amd64', os: 'linux', commands: { python: `Python 3.${number}`, git: `git ${number}` }, build: { builder: 'openhands.agent_server.docker.build', platform: 'linux/amd64' } },
    reference_count: number === 25 ? 50 : 0, run_reference_count: number === 25 ? 25 : 0, flow_run_reference_count: number === 25 ? 23 : 0, created_at: now };
}
async function json(route: Route, body: unknown, status = 200) { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }); }
async function setup(page: Page, options: { failDelete?: string; active?: boolean; longCommands?: boolean; failRuns?: boolean } = {}) {
  let versions = Array.from({ length: 25 }, (_, index) => version(25 - index));
  const selectedBases: string[] = []; const deletions: string[] = []; const runRequests: string[] = []; const directoryRequests: string[] = [];
  let failRuns = options.failRuns;
  if (options.longCommands) versions.find(item => item.id === 'version-24')!.manifest.commands = Object.fromEntries(Array.from({ length: 40 }, (_, index) => [`tool-${index + 1}`, `Version ${index + 1}\n${'Long output '.repeat(18)}`])) as ReturnType<typeof version>['manifest']['commands'];
  let active: Array<Record<string, unknown>> = options.active ? [{ id: 'existing-setup', environment_id: 'environment-1', state: 'RUNNING', base_version_id: 'version-24', expires_at: '2027-01-01T00:00:00Z' }] : [];
  const environment = () => ({ id: 'environment-1', name: '开发环境', description: '用于历史版本管理', row_version: 1, versions, active_sessions: active, created_at: now, updated_at: now });
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method();
    if (path.endsWith('/auth/me')) return json(route, { id: 'environment-manager', username: 'manager', role: 'USER' });
    if (path.endsWith('/terminal-environments')) return json(route, [environment()]);
    if (path.endsWith('/flows')) return json(route, [{ id: 'flow-1', name: '定位流程', description: '', row_version: 1, nodes: [], edges: [] }]);
    if (path.endsWith('/flow-runs')) return json(route, Array.from({ length: 60 }, (_, index) => ({ id: `run-${index + 1}`, flow_definition_id: 'flow-1', flow_name: '定位流程', run_no: index + 1, name: `FlowRun ${index + 1}`, state: 'ACTIVE', run_mode: 'MANUAL', runtime_status: 'STOPPED', runtime_write_available: false, environment_version_id: 'version-25', active_snapshot_version: 1, started_at: now, updated_at: now })));
    if (path.endsWith('/records')) { directoryRequests.push(path); return json(route, { items: [], next_cursor: null }); }

    if (path.includes('/versions/') && path.endsWith('/runs')) {
      runRequests.push(route.request().url());
      if (failRuns) return json(route, { error: { code: 'UNAVAILABLE', message: '暂时不可用' } }, 503);
      const currentPage = Number(new URL(route.request().url()).searchParams.get('page') || 1);
      const all = Array.from({ length: 23 }, (_, index) => ({ id: `run-${index + 1}`, flow_definition_id: 'flow-1', name: `FlowRun ${index + 1}`, run_no: index + 1, state: index ? 'COMPLETED' : 'ACTIVE', run_mode: index ? 'AUTOMATIC' : 'MANUAL', parent_flow_run_id: null, started_at: now, finished_at: null }));
      return json(route, { items: all.slice((currentPage - 1) * 20, currentPage * 20), total: all.length, page: currentPage, page_size: 20 });
    }
    if (path.endsWith('/setup-sessions') && method === 'POST') {
      const id = route.request().postDataJSON().base_version_id; selectedBases.push(id);
      active = [{ id: 'setup-new', environment_id: 'environment-1', state: 'RUNNING', base_version_id: id, expires_at: '2027-01-01T00:00:00Z' }];
      return json(route, active[0], 201);
    }
    if (path.includes('/versions/') && method === 'DELETE') {
      const id = path.split('/').at(-1)!; deletions.push(id);
      if (id === options.failDelete && deletions.filter(item => item === id).length === 1) return json(route, { error: { code: 'ENVIRONMENT_VERSION_IN_USE', message: '版本已被新运行引用', details: { run_reference_count: 1 } } }, 409);
      versions = versions.filter(item => item.id !== id); return route.fulfill({ status: 204 });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.routeWebSocket('**/environment-setup-sessions/*/terminal*', socket => socket.send('connected\r\n$ '));
  await page.goto('/'); await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '终端环境', exact: true }).click();
  await expect(page.getByRole('region', { name: '环境版本列表' })).toBeVisible();
  return { selectedBases, deletions, runRequests, directoryRequests, allowRuns: () => { failRuns = false; } };
}

test('history fills the detail pane and keeps a compact header, sticky columns, and button pagination', async ({ page }) => {
  await setup(page);
  const panel = page.getByRole('region', { name: '环境版本列表' }); const rows = panel.locator('tbody tr');
  await expect(rows).toHaveCount(20);
  await expect(page.locator('.settings-environment-tools,.settings-environment-summary')).toHaveCount(0);
  for (const width of [1920, 1440, 1024]) {
    await page.setViewportSize({ width, height: 600 });
    const metrics = await panel.evaluate(element => {
      const header = element.querySelector('.environment-versions-toolbar')!; const scroll = element.querySelector('.environment-versions-table-scroll')!;
      const row = element.querySelector('tbody tr')!; const footer = element.querySelector('footer')!;
      return { toolbar: header.getBoundingClientRect().height, row: row.getBoundingClientRect().height,
        footer: footer.getBoundingClientRect().bottom, viewport: innerHeight, body: document.documentElement.scrollHeight,
        scroll: scroll.scrollHeight, client: scroll.clientHeight };
    });
    expect(metrics.toolbar).toBeLessThanOrEqual(50); expect(metrics.row).toBeLessThanOrEqual(60);
    expect(metrics.footer).toBeLessThanOrEqual(metrics.viewport); expect(metrics.body).toBeLessThanOrEqual(metrics.viewport);
    expect(metrics.scroll).toBeGreaterThan(metrics.client);
    await panel.locator('.environment-versions-table-scroll').hover(); await page.mouse.wheel(0, 400);
    await expect.poll(() => panel.locator('.environment-versions-table-scroll').evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    await expect(panel.getByRole('columnheader', { name: '版本与说明' })).toBeInViewport();
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(rows).toHaveCount(5);
    await expect.poll(() => panel.locator('.environment-versions-table-scroll').evaluate(node => node.scrollTop)).toBe(0);
    await panel.getByRole('button', { name: '上一页', exact: true }).click();
    await expect(rows).toHaveCount(20);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await panel.getByRole('textbox', { name: '搜索历史版本' }).fill('版本 19 说明');
  await expect(rows).toHaveCount(1); await expect(rows.first()).toContainText('v19');
  await panel.getByRole('textbox', { name: '搜索历史版本' }).fill('');
  await expect(rows).toHaveCount(20);
  await page.screenshot({ path: test.info().outputPath('environment-version-list.png') });
});

test('any READY historical version sends its own base id and an active setup prevents a second draft', async ({ page }) => {
  const state = await setup(page);
  const panel = page.getByRole('region', { name: '环境版本列表' });
  await panel.getByRole('button', { name: '从 v24 创建草稿', exact: true }).click();
  await expect.poll(() => state.selectedBases).toEqual(['version-24']);
  await expect(page.getByRole('heading', { name: '环境配置终端' })).toBeVisible();
  await page.getByRole('button', { name: '关闭视图' }).click();
  await expect(panel.getByRole('button', { name: '从 v23 创建草稿', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '继续配置', exact: true })).toBeVisible();
  await expect(panel.getByRole('checkbox', { name: '选择版本 v24', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '继续配置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '环境配置终端' })).toBeVisible();
  expect(state.selectedBases).toHaveLength(1);
});

test('version details use the chosen immutable command inventory, show unknown authentication, and support keyboard tabs', async ({ page }) => {
  await setup(page);
  const panel = page.getByRole('region', { name: '环境版本列表' });
  await panel.locator('tbody tr').filter({ hasText: '版本 24 说明' }).getByText('版本 24 说明', { exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '开发环境 v24 版本详情' });
  await expect(dialog.getByText('test/runtime:v24', { exact: true })).toBeVisible();
  await dialog.getByRole('tab', { name: '命令（2）', exact: true }).click();
  await expect(dialog.getByText('Python 3.24', { exact: true })).toBeVisible();
  await expect(dialog.getByText('Python 3.25', { exact: true })).toHaveCount(0);
  await dialog.getByRole('tab', { name: '命令（2）', exact: true }).press('ArrowRight');
  await expect(dialog.getByRole('tab', { name: '认证信息', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(dialog.getByText('此版本未记录认证清单，无法确认镜像中包含哪些认证信息。', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('environment-version-auth.png') });
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
  await expect(panel.getByRole('checkbox', { name: '选择版本 v24', exact: true })).not.toBeChecked();
});

test('bulk delete spans pages, skips protected versions, and keeps failures selected for retry', async ({ page }) => {
  const state = await setup(page, { failDelete: 'version-23' });
  const panel = page.getByRole('region', { name: '环境版本列表' });
  await panel.getByRole('checkbox', { name: '选择全部可删除版本' }).check();
  await expect(panel.getByRole('checkbox', { name: '选择版本 v25', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: '删除所选 (24)' })).toBeEnabled();
  await panel.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(panel.getByRole('checkbox', { name: '选择版本 v1', exact: true })).toBeChecked();
  await panel.getByRole('button', { name: '删除所选 (24)' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除版本', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('已删除 23 个环境版本');
  await expect(panel.getByRole('alert')).toContainText('1 个版本未删除');
  await expect(panel.locator('tbody tr')).toHaveCount(2);
  await expect(panel.getByRole('checkbox', { name: '选择版本 v23', exact: true })).toBeChecked();
  await expect(panel.getByRole('button', { name: '删除所选 (1)' })).toBeEnabled();
  expect(state.deletions).toHaveLength(24); expect(new Set(state.deletions).size).toBe(24);
  expect(state.deletions).not.toContain('version-25');
  await panel.getByRole('button', { name: '删除所选 (1)' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除版本', exact: true }).click();
  await expect(panel.locator('tbody tr')).toHaveCount(1);
  await expect(panel.getByRole('button', { name: '删除所选 (0)' })).toBeDisabled();
  expect(state.deletions).toHaveLength(25);
  expect(state.deletions.at(-1)).toBe('version-23');
});


test('record click opens details independently of checkboxes and removes the redundant action and risk banner', async ({ page }) => {
  await setup(page);
  const panel = page.getByRole('region', { name: '环境版本列表' });
  await expect(page.getByText('凭据风险 · 发布可能保留认证信息', { exact: true })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /^版本 v\d+ 详情$/ })).toHaveCount(0);
  await panel.getByRole('checkbox', { name: '选择版本 v24', exact: true }).check();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await panel.locator('tbody tr').filter({ hasText: '版本 25 说明' }).getByText('版本 25 说明', { exact: true }).click();
  await expect(page.getByRole('dialog', { name: '开发环境 v25 版本详情' })).toBeVisible();
  await page.keyboard.press('Escape');
  const row = panel.locator('tbody tr').filter({ hasText: '版本 24 说明' });
  await row.focus(); await row.press('Enter');
  await expect(page.getByRole('dialog', { name: '开发环境 v24 版本详情' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(panel.getByRole('checkbox', { name: '选择版本 v24', exact: true })).toBeChecked();
});

test('long command output scrolls under a fixed header within its own viewport', async ({ page }) => {
  await setup(page, { longCommands: true });
  await page.getByRole('button', { name: '查看版本 v24', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '开发环境 v24 版本详情' });
  await dialog.getByRole('tab', { name: '命令（40）', exact: true }).click();
  const scroll = dialog.locator('.environment-version-detail-table-scroll');
  const note = dialog.locator('.environment-version-detail-note');
  const before = await note.boundingBox();
  await scroll.evaluate(node => { node.scrollTop = 137; });
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBe(137);
  const positions = await scroll.evaluate(node => {
    const header = node.querySelector('thead')!.getBoundingClientRect();
    const bounds = node.getBoundingClientRect();
    const topCell = document.elementFromPoint(header.left + 20, bounds.top + 5);
    return { top: bounds.top, header: header.top, height: header.height, topIsHeader: Boolean(topCell?.closest('thead')) };
  });
  expect(positions.header).toBeCloseTo(positions.top, 0);
  expect(positions.topIsHeader).toBe(true); expect(positions.height).toBeGreaterThan(20);
  expect((await note.boundingBox())!.y).toBe(before!.y);
  await expect(dialog.getByRole('button', { name: '完成', exact: true })).toBeInViewport();
  await page.screenshot({ path: test.info().outputPath('environment-version-command-scroll.png') });
});

test('version references load on demand, retry errors, and paginate all matching runs', async ({ page }) => {
  const state = await setup(page, { failRuns: true });
  await page.getByRole('button', { name: '查看版本 v25', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '开发环境 v25 版本详情' });
  expect(state.runRequests).toHaveLength(0);
  await dialog.getByRole('tab', { name: 'FlowRun（23）', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('加载 FlowRun 失败');
  state.allowRuns();
  await dialog.getByRole('button', { name: '重试', exact: true }).click();
  await expect(dialog.locator('tbody tr')).toHaveCount(20);
  await expect(dialog.getByRole('columnheader', { name: '类型', exact: true })).toHaveCount(0);
  await expect(dialog.getByText('子运行', { exact: true })).toHaveCount(0);
  await expect(dialog.getByText('FlowRun 1', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(dialog.locator('tbody tr')).toHaveCount(3);
  await expect(dialog.getByText('FlowRun 21', { exact: true })).toBeVisible();
  expect(state.runRequests.every(url => url.includes('/versions/version-25/runs'))).toBe(true);
  expect(state.runRequests.at(-1)).toContain('page=2');
  await page.screenshot({ path: test.info().outputPath('environment-version-runs.png') });
});


test('FlowRun reference jumps to the Runs tab, expands the flow and scrolls to the chosen FlowRun', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  const state = await setup(page);
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText: '定位流程' }).click();
  await page.locator('.flow-run-tree-run').filter({ hasText: 'Run #1 ·' }).click();
  await page.getByRole('searchbox', { name: '搜索运行记录', exact: true }).fill('旧筛选');
  await page.getByRole('navigation', { name: '运行记录类型' }).getByRole('button', { name: '连续', exact: true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText: '定位流程' }).click();
  await expect(page.locator('.flow-run-tree-select')).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('button', { name: '终端环境', exact: true }).click();
  await page.getByRole('button', { name: '查看版本 v25', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '开发环境 v25 版本详情' });
  await dialog.getByRole('tab', { name: 'FlowRun（23）', exact: true }).click();
  await dialog.getByRole('button', { name: '下一页', exact: true }).click();
  await dialog.getByRole('button', { name: '定位 FlowRun FlowRun 23', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '流程运行', exact: true })).toHaveClass(/active/);
  await expect(page.locator('.flow-run-tree-select')).toHaveAttribute('aria-expanded', 'true');
  const selected = page.locator('.flow-run-tree-run').filter({ hasText: 'Run #23 ·' });
  await expect(selected).toHaveClass(/active/); await expect(selected).toBeInViewport();
  await expect(selected).toBeFocused();
  await expect(page.locator('.run-record-directory-header h3')).toHaveText('FlowRun 23');
  await expect(page.getByRole('searchbox', { name: '搜索运行记录', exact: true })).toHaveValue('');
  await expect(page.getByRole('navigation', { name: '运行记录类型' }).getByRole('button', { name: '全部', exact: true })).toHaveClass(/active/);
  await expect.poll(() => state.directoryRequests.some(path => path.includes('/run-23/'))).toBe(true);
  await page.screenshot({ path: test.info().outputPath('environment-flowrun-located.png') });
  await page.getByRole('button', { name: '终端环境', exact: true }).click();
  await page.getByRole('button', { name: '查看版本 v25', exact: true }).click();
  await dialog.getByRole('tab', { name: 'FlowRun（23）', exact: true }).click();
  await dialog.locator('tbody tr').first().getByText('进行中', { exact: true }).click();
  await expect(page.locator('.run-record-directory-header h3')).toHaveText('FlowRun 1');
  await expect(page.locator('.flow-run-tree-run').filter({ hasText: 'Run #1 ·' })).toHaveClass(/active/);
  await expect(page.locator('.flow-run-tree-run').filter({ hasText: 'Run #1 ·' })).toBeInViewport();

});
