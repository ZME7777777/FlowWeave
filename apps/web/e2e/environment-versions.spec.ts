import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-29T01:00:00Z';
function version(number: number) {
  return { id: `version-${number}`, environment_id: 'environment-1', version_no: number, parent_version_id: number > 1 ? `version-${number - 1}` : null,
    state: 'READY', image_reference: `test/runtime:v${number}`, image_digest: 'sha256:' + String(number).padStart(64, '0'), base_image_reference: 'test/base:1', base_image_digest: 'sha256:' + 'a'.repeat(64),
    description: `版本 ${number} 说明`, runtime_capabilities: ['browser'], manifest: { architecture: 'amd64', os: 'linux', commands: { python: `Python 3.${number}`, git: `git ${number}` }, build: { builder: 'openhands.agent_server.docker.build', platform: 'linux/amd64' } },
    reference_count: number === 25 ? 2 : 0, run_reference_count: number === 25 ? 2 : 0, created_at: now };
}
async function json(route: Route, body: unknown, status = 200) { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }); }
async function setup(page: Page, options: { failDelete?: string; active?: boolean } = {}) {
  let versions = Array.from({ length: 25 }, (_, index) => version(25 - index));
  const selectedBases: string[] = []; const deletions: string[] = [];
  let active: Array<Record<string, unknown>> = options.active ? [{ id: 'existing-setup', environment_id: 'environment-1', state: 'RUNNING', base_version_id: 'version-24', expires_at: '2027-01-01T00:00:00Z' }] : [];
  const environment = () => ({ id: 'environment-1', name: '开发环境', description: '用于历史版本管理', row_version: 1, versions, active_sessions: active, created_at: now, updated_at: now });
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method();
    if (path.endsWith('/auth/me')) return json(route, { id: 'environment-manager', username: 'manager', role: 'USER' });
    if (path.endsWith('/terminal-environments')) return json(route, [environment()]);
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
  return { selectedBases, deletions };
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
  await panel.getByRole('button', { name: '查看版本 v24', exact: true }).click();
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
