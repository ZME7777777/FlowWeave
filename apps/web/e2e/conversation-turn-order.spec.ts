import { expect, test } from '@playwright/test';

const initialTimestamp = '2026-10-12T09:59:00Z';
const olderTimestamp = '2026-10-12T10:00:00Z';
const newerTimestamp = '2026-10-12T10:02:00Z';

test('Conversation renders user turns by message time when events arrive out of order', async ({ page }) => {
  let authenticated = false;
  const user = { id: 'turn-order-user', username: 'turn-order-user', role: 'USER', is_super_admin: false };
  const workspace = { id: 'turn-order-workspace', display_name: '回合顺序工作区', desired_state: 'RUNNING', updated_at: initialTimestamp };
  const conversation = {
    id: 'turn-order-conversation', display_title: '乱序回合', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: initialTimestamp, updated_at: initialTimestamp,
  };
  const events = [
    { id: 'first-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '第一条消息', timestamp: initialTimestamp } },
    { id: 'first-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'first-user', content: '第一条回复', timestamp: '2026-10-12T09:59:30Z' } },
    // Runtime recovery can return a later turn before an earlier one.
    { id: 'later-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'first-reply', content: '后发送的消息', timestamp: newerTimestamp } },
    { id: 'earlier-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'first-reply', content: '先发送的消息', timestamp: olderTimestamp } },
    { id: 'earlier-action', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'earlier-user', action_id: 'earlier-action', tool_call_id: 'earlier-call', event_name: 'TerminalAction', tool_name: 'terminal', content: '正在处理先发送的消息', details: { command: 'pwd' }, timestamp: '2026-10-12T10:00:10Z' } },
    { id: 'earlier-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'earlier-action', content: '先发送消息的回复', timestamp: '2026-10-12T10:00:20Z' } },
  ];
  const batch = { events, next_cursor: 'earlier-reply', history_cursor: null, result: { status: 'COMPLETED' } };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return authenticated ? json(user) : json({ error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(user); }
    if (path.endsWith('/agent-workspaces/default')) return json(workspace);
    if (path.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: initialTimestamp });
    if (path.endsWith('/conversations')) return json({ items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) return json({ events: batch, context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/events')) return json(batch);
    if (path.endsWith('/input-readiness')) return json({ ready: true, execution_status: 'idle' });
    if (path.endsWith('/pending-confirmation')) return json({ pending: false });
    if (path.endsWith('/context')) return json({ model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true });
    if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/model-providers')) return json([{ id: 'turn-order-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json([]);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(conversation);
    return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByLabel('账号').fill(user.username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto(`/agent/conversations/${conversation.id}`);

  await expect.poll(() => page.locator('.conversation-turn [data-user-event-id]').evaluateAll(messages => messages.map(message => message.getAttribute('data-user-event-id')))).toEqual([
    'first-user',
    'earlier-user',
    'later-user',
  ]);
  await expect(page.locator('[data-conversation-turn="earlier-user"] [data-event-id="earlier-reply"]')).toBeVisible();
});
