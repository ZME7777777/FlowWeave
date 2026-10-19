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
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true })
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true }); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/conversation-schedules')) return json(route, []);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: {}, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByLabel('账号').fill('schedule-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await page.getByRole('button', { name: '会话定时任务' }).click();

  await expect(page.getByRole('dialog', { name: '会话定时任务' })).toBeVisible();
  await expect(page.getByText('日报会话', { exact: true })).toBeVisible();
  await page.getByRole('dialog', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新建会话定时任务' })).toBeVisible();
  await expect(page.getByLabel('目标会话')).toHaveValue('scheduled-conversation');
});
