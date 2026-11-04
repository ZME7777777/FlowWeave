import { expect, test } from '@playwright/test';

const user = { id: 'case-actions-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const plan = (id: string, action: string, reason: string, created: string) => ({ id, case_id: 'c1', stage_id: 's1', action, state: 'SUCCEEDED', reason, error_code: null, retry_of_plan_id: null, execution_flow_run_id: null, created_at: created });
const base = { id: 'c1', name: '提问场景', objective: '目标', description: '', materials: [], work_directory_id: null, row_version: 3, stages: [], flow_run_ids: [], tasks: [], decision_proposals: [], feedback_deliveries: [], control_rounds: [], control_conversation_binding_id: 'b1', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' };

test('case workspace lists every Agent action and surfaces a pending question', async ({ page }) => {
  let item: Record<string, unknown> = {
    ...base, state: 'PAUSED', pause_reason: 'WAITING_HUMAN',
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
    if (path === '/cases/c1/coordination' || path.startsWith('/cases/c1/coordination')) { resumed += 1; item = { ...item, state: 'ACTIVE', pause_reason: null, row_version: 4 }; return route.fulfill({ json: item }); }
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
  await expect(dialog).not.toContainText('结果可信，接受');
  await expect(dialog.getByRole('button', { name: '在会话中查看' }).first()).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await question.getByRole('button', { name: '已回答，继续' }).click();
  await expect.poll(() => resumed).toBe(1);
  await expect(question).toBeHidden();
});

test('an Agent conclusion proposal needs a human decision and cannot be bypassed by resuming', async ({ page }) => {
  let item: Record<string, unknown> = {
    ...base, state: 'PAUSED', pause_reason: 'CONCLUSION_PROPOSED', pause_note: '目标已回答，后续阶段没有必要',
    control_plans: [plan('p1', 'CONCLUDE_CASE', '目标已回答，后续阶段没有必要', '2026-01-03T00:00:00Z')],
  };
  const calls: Array<{ path: string; body: unknown }> = [];
  await page.route('**/api/v1/**', route => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/v1/, '');
    if (path === '/auth/me') return route.fulfill({ json: user });
    if (path === '/cases' && req.method() === 'GET') return route.fulfill({ json: [item] });
    if (path === '/cases/c1/conclusion/reject') { calls.push({ path, body: req.postDataJSON() }); item = { ...item, state: 'ACTIVE', pause_reason: null, pause_note: null, row_version: 4 }; return route.fulfill({ json: item }); }
    if (path === '/cases/c1/conclusion/confirm') { calls.push({ path, body: req.postDataJSON() }); item = { ...item, state: 'COMPLETED', pause_reason: null, row_version: 4 }; return route.fulfill({ json: item }); }
    if (path === '/cases/c1') return route.fulfill({ json: item });
    if (path.includes('default-agent-workspace')) return route.fulfill({ json: { id: 'ws' } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '场景空间' }).click();
  await page.locator('.scenario-tree').getByText('提问场景').click();
  const proposal = page.getByRole('status', { name: 'Agent 提议结束场景' });
  await expect(proposal).toContainText('目标已回答');
  await expect(page.getByRole('button', { name: '继续协调' })).toBeDisabled();
  await proposal.getByLabel('继续的原因（可选）').fill('还需要 B 区域');
  await proposal.getByRole('button', { name: '继续' }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0]).toEqual({ path: '/cases/c1/conclusion/reject', body: { expected_row_version: 3, note: '还需要 B 区域' } });
  await expect(proposal).toBeHidden();
});
