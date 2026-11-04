import { expect, test } from '@playwright/test';

const user = { id: 'case-actions-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const plan = (id: string, action: string, reason: string, created: string) => ({ id, case_id: 'c1', stage_id: 's1', action, state: 'SUCCEEDED', reason, error_code: null, retry_of_plan_id: null, execution_flow_run_id: null, created_at: created });
const base = { id: 'c1', name: '提问场景', objective: '目标', description: '', materials: [], work_directory_id: null, row_version: 3, stages: [], flow_run_ids: [], tasks: [], decision_proposals: [], feedback_deliveries: [], control_rounds: [], control_conversation_binding_id: 'b1', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' };

test('case workspace lists every Agent action and surfaces a pending question', async ({ page }) => {
  let item: Record<string, unknown> = {
    ...base, state: 'PAUSED',
    control_plans: [
      plan('p1', 'ACCEPT_STEPWISE_ATTEMPT', '结果可信，接受', '2026-01-02T00:00:00Z'),
      plan('p2', 'ASK_HUMAN', '空结果：确实没有，还是范围找错了？推荐方案 A', '2026-01-03T00:00:00Z'),
    ],
  };
  let resumed = 0;
  await page.route('**/api/v1/**', route => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/v1/, '');
    if (path === '/auth/me') return route.fulfill({ json: user });
    if (path === '/cases' && req.method() === 'GET') return route.fulfill({ json: [item] });
    if (path === '/cases/c1/coordination' || path.startsWith('/cases/c1/coordination')) { resumed += 1; item = { ...item, state: 'ACTIVE', row_version: 4 }; return route.fulfill({ json: item }); }
    if (path === '/cases/c1') return route.fulfill({ json: item });
    if (path.includes('default-agent-workspace')) return route.fulfill({ json: { id: 'ws' } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '场景空间' }).click();
  await page.locator('.scenario-tree').getByText('提问场景').click();
  const question = page.getByRole('status', { name: 'Agent 正在等待你的回答' });
  await expect(question).toContainText('空结果：确实没有');
  await page.getByRole('button', { name: 'Agent 动作' }).click();
  const dialog = page.getByRole('dialog', { name: 'Agent 动作' });
  await expect(dialog).toContainText('向人工提问');
  await expect(dialog).toContainText('接受节点结果');
  await expect(dialog).toContainText('结果可信，接受');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await question.getByRole('button', { name: '已回答，继续' }).click();
  await expect.poll(() => resumed).toBe(1);
  await expect(question).toBeHidden();
});
