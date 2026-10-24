import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-09-12T09:30:00Z';
const user = {
  id: '00000000-0000-0000-0000-000000000021',
  username: 'cache-user',
  role: 'USER',
  is_super_admin: false,
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function login(page: Page) {
  await page.getByLabel('账号').fill(user.username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByRole('button', { name: '账户与设置' })).toContainText(user.username);
}

async function failedToolSession(page: Page, initialStatus: string) {
  let authenticated = false;
  let status = initialStatus;
  let interruptPosts = 0;
  let messagePosts = 0;
  let newUserVisible = false;
  const workspace = { id: 'failed-tool-workspace', display_name: '失败工具工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = { id: 'failed-tool-conversation', display_title: '未闭环工具会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now };
  const events = () => ({
    events: [
      { id: 'failed-tool-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '检查当前工作区', timestamp: now } },
      { id: 'failed-tool-action', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'failed-tool-user', action_id: 'failed-tool-action', tool_call_id: 'failed-tool-call', tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'git status --short' }, timestamp: now } },
      { id: 'failed-task-action', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'failed-tool-action', action_id: 'failed-task-action', tool_call_id: 'failed-task-call', tool_name: 'task', event_name: 'TaskAction', runtime_task: { phase: 'REQUESTED', action_event_id: 'failed-task-action', tool_call_id: 'failed-task-call', subagent_type: 'general-purpose', description: '检查子任务' }, timestamp: now } },
      { id: 'failed-tool-error', event_type: 'ERROR', payload: { source_type: 'AgentErrorEvent', source: 'agent', parent_id: 'failed-tool-action', content: '[Errno 11] Resource temporarily unavailable', timestamp: now } },
      { id: 'failed-tool-restart', event_type: 'ERROR', payload: { source_type: 'AgentErrorEvent', source: 'agent', parent_id: 'failed-tool-error', content: 'The agent server restarted while the tool was in progress. The tool did not complete.', timestamp: now } },
      ...(newUserVisible ? [{ id: 'new-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'failed-tool-restart', content: '重新尝试当前任务', timestamp: now } }] : []),
    ],
    next_cursor: newUserVisible ? 'new-user' : 'failed-tool-restart', history_cursor: null, result: { status: status === 'running' ? 'RUNNING' : 'FAILED' },
  });
  const readiness = () => ({ ready: status !== 'running', execution_status: status });
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations')) return json(route, { items: [conversation], next_cursor: null });
    // Deliberately stale: the selected binding's native readiness must win.
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [conversation.id] });
    if (path.endsWith('/hydration')) return json(route, { events: events(), context: {}, readiness: readiness() });
    if (path.endsWith('/interrupt')) { interruptPosts += 1; status = 'error'; return json(route, { accepted: true }, 202); }
    if (path.endsWith('/messages') && request.method() === 'POST') { messagePosts += 1; return json(route, { accepted: true, cursor: 'new-user' }, 202); }
    if (path.endsWith('/events')) return json(route, events());
    if (path.endsWith('/input-readiness')) return json(route, readiness());
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/context')) return json(route, {});
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'failed-tool-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/')) return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${conversation.id}`);
  return { revealNewUser: () => { newUserVisible = true; status = 'running'; }, fail: () => { status = 'error'; }, row: page.locator(`[data-conversation-binding-id="${conversation.id}"]`), interruptPosts: () => interruptPosts, messagePosts: () => messagePosts };
}

for (const status of ['error', 'stuck']) {
  test(`Native ${status} closes an unfinished tool turn without a final reply`, async ({ page }) => {
    const session = await failedToolSession(page, status);
    await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
    await expect(page.getByText('检查当前工作区', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
    await expect(session.row.locator('.agent-workspace-conversation-running')).toHaveCount(0);
    await expect(page.locator('.conversation-turn-status-dots')).toHaveCount(0);
    await page.locator('summary[aria-label="展开工作过程"]').click();
    await expect(page.getByText('结果未返回 git status --short', { exact: true })).toBeVisible();
    await expect(page.locator('.agent-workspace-subagent-summary')).toContainText('1 个任务');
    await page.locator('.agent-workspace-subagents button').click();
    await expect(page.locator('.agent-subagent-status')).toHaveText('主会话异常结束，结果未返回');
    await expect(page.getByText('正在运行 git status --short', { exact: true })).toHaveCount(0);
    // Silent formal-event retries must never resurrect the failed turn.
    await page.waitForTimeout(9_000);
    await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
    await expect(session.row.locator('.agent-workspace-conversation-running')).toHaveCount(0);
    expect(session.messagePosts()).toBe(0);
  });
}

test('Pause converges when native readiness reports error instead of paused', async ({ page }) => {
  const session = await failedToolSession(page, 'running');
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('保留的排队消息');
  await composer.press('Enter');
  await expect(page.getByLabel('消息投递队列').getByText('保留的排队消息')).toBeVisible();
  await page.getByRole('button', { name: '暂停当前 Agent' }).click();
  await expect.poll(session.interruptPosts).toBe(1);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '暂停请求已发送' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '继续运行 Agent' })).toHaveCount(0);
  await expect(session.row.locator('.agent-workspace-conversation-running')).toHaveCount(0);
  await expect(page.locator('.conversation-turn-status-dots')).toHaveCount(0);
  await expect(page.getByLabel('消息投递队列').getByText('保留的排队消息')).toBeVisible();
  expect(session.messagePosts()).toBe(0);
});

test('Recoverable AgentErrorEvent keeps a natively running turn active', async ({ page }) => {
  const session = await failedToolSession(page, 'running');
  await expect(page.getByText('检查当前工作区', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(session.row.locator('.agent-workspace-conversation-running')).toBeVisible();
});


test('A new submission bridges stale failure readiness and closes on a fresh native failure', async ({ page }) => {
  const session = await failedToolSession(page, 'error');
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(composer).toBeEditable();
  await expect(page.getByText('检查当前工作区', { exact: true })).toBeVisible();
  await composer.fill('重新尝试当前任务');
  await composer.press('Enter');
  await expect.poll(session.messagePosts).toBe(1);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  session.revealNewUser();
  const runningReadiness = page.waitForResponse(response => response.url().endsWith('/input-readiness'));
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await runningReadiness;
  await expect(page.locator('[data-user-event-id="new-user"]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  session.fail();
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  await expect(session.row.locator('.agent-workspace-conversation-running')).toHaveCount(0);
  expect(session.messagePosts()).toBe(1);
});
