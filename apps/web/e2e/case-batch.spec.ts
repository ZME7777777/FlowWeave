import { expect, test } from '@playwright/test';

const user = { id: 'case-batch-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const mk = (id: string, name: string) => ({ id, name, objective: '', description: '', materials: [], work_directory_id: null, state: 'DRAFT', row_version: 1, stages: [], flow_run_ids: [], tasks: [], control_plans: [], decision_proposals: [], feedback_deliveries: [], control_rounds: [], created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' });

test('case list supports batch copy, export and delete', async ({ page }) => {
  let cases = [mk('c1', '场景甲'), mk('c2', '场景乙'), mk('c3', '场景丙')];
  const deleted: string[] = [];
  let created = 0;
  await page.route('**/api/v1/**', route => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/v1/, '');
    if (path === '/auth/me') return route.fulfill({ json: user });
    if (path === '/cases' && req.method() === 'GET') return route.fulfill({ json: cases });
    if (path === '/cases' && req.method() === 'POST') { created += 1; const item = mk(`n${created}`, JSON.parse(req.postData()!).name); cases = [item, ...cases]; return route.fulfill({ status: 201, json: item }); }
    const one = path.match(/^\/cases\/([^/]+)$/);
    if (one && req.method() === 'GET') return route.fulfill({ json: cases.find(item => item.id === one[1]) });
    if (one && req.method() === 'DELETE') { deleted.push(one[1]); cases = cases.filter(item => item.id !== one[1]); return route.fulfill({ status: 204, body: '' }); }
    if (path.includes('default-agent-workspace')) return route.fulfill({ json: { id: 'ws' } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '场景空间' }).click();
  await expect(page.locator('.scenario-tree').getByText('场景甲')).toBeVisible();
  await page.getByRole('button', { name: '批量管理' }).click();
  await page.getByRole('checkbox', { name: '选择场景 场景甲' }).check();
  await page.getByRole('checkbox', { name: '选择场景 场景乙' }).check();
  await expect(page.getByText('已选 2')).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '批量导出' }).click();
  expect((await download).suggestedFilename()).toBe('cases-2.json');
  await page.getByRole('button', { name: '批量复制' }).click();
  await expect(page.locator('.scenario-tree').getByText('场景甲 副本')).toBeVisible();
  expect(created).toBe(2);
  await page.getByRole('button', { name: '批量管理' }).click();
  await page.getByRole('checkbox', { name: '选择场景 场景丙' }).check();
  await page.getByRole('button', { name: '批量删除' }).click();
  await page.getByRole('button', { name: '批量删除' }).last().click();
  await expect.poll(() => deleted).toEqual(['c3']);
});
