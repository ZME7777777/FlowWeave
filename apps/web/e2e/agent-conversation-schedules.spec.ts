import { expect, test, type Route } from '@playwright/test';

const now = '2026-10-19T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('standalone Agent Workspace exposes a separate conversation schedule panel', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'schedule-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'scheduled-conversation', display_title: '日报会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const schedules = [{ id: 'daily-schedule', workspace_id: workspace.id, binding_id: null, work_directory_id: 'automation-directory', work_directory_name: '自动化 · 每日项目检查', workspace_mode: 'AUTOMATION', conversation_mode: 'NEW', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'medium', name: '每日项目检查', prompt: '请检查项目状态。', cron_expression: '0 9 * * 1-5', status: 'ACTIVE', next_run_at: '2026-10-20T01:00:00Z', last_run_at: now, row_version: 1, created_at: now, updated_at: now }];
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true })
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true }); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/conversation-management')) return json(route, { items: [], truncated: false });
    if (path.endsWith('/conversation-schedules')) return json(route, schedules);
    if (path.includes('/conversation-schedules/') && path.endsWith('/occurrences')) return json(route, [{ id: 'delivery-1', binding_id: 'scheduled-run-1', conversation_title: '每日项目检查 · 2026-10-19 09:00', scheduled_for: now, trigger_kind: 'SCHEDULED', state: 'STARTED', message_cursor: 'event-1', created_at: now }]);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: {}, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [{ id: 'existing-directory', display_name: '已有项目目录' }] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'provider-1', name: '测试供应商', connection_state: 'CONNECTED', models: [{ model_name: 'model-1', enabled: true, is_default: true, default_reasoning_effort: 'medium', supported_reasoning_efforts: ['low', 'medium', 'high'] }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByLabel('账号').fill('schedule-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '账户与设置' }).click();
  await expect(page.getByRole('menuitem', { name: '会话管理' })).toHaveCount(0);
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await expect(page.getByRole('button', { name: '打开工作区工具' })).toBeVisible();
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  await expect(page.getByRole('region', { name: '会话定时任务' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(page.locator('.agent-schedule-run').filter({ hasText: '每日项目检查 · 2026-10-19 09:00' })).toBeVisible();
  await page.getByRole('button', { name: /每日项目检查 操作/ }).click();
  await expect(page.getByRole('menuitem', { name: '暂停' })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: '立即运行' })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: '编辑' })).toBeVisible();
  await page.getByRole('region', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新建定时任务' })).toBeVisible();
  await expect(page.getByLabel('工作区模式')).toHaveValue('AUTOMATION');
  await expect(page.getByLabel('运行方式')).toHaveValue('NEW');
  await expect(page.getByLabel('模型供应商')).toHaveValue('provider-1');
  await expect(page.getByLabel('模型名称')).toHaveValue('model-1');
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '会话管理' }).click();
  await expect(page.getByRole('region', { name: '会话管理' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '会话管理', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '会话管理', exact: true })).toHaveCSS('font-size', '21px');
});
