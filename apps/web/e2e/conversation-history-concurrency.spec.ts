import { expect, test, type Page, type Route, type WebSocketRoute } from '@playwright/test';

const now = '2026-09-29T09:30:00Z';
const user = {
  id: '00000000-0000-0000-0000-000000000029',
  username: 'scroll-stability-user',
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

test('history prefetch yields on hidden tabs, resumes fully, and does not retry workspace 503', async ({ page }) => {
  await page.clock.install();
  let authenticated = false;
  const historyRequests: { cursor: string; at: number }[] = [];
  let workspaceRequests = 0;
  let stream: WebSocketRoute | undefined;
  const workspace = {
    id: 'scroll-stability-workspace', display_name: '滚动稳定工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'scroll-stability-conversation', display_title: '滚动稳定会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running',
    created_at: now, updated_at: now,
  };
  const event = (id: string, eventType: string, payload: Record<string, unknown>) => ({
    id, event_type: eventType, payload: { timestamp: now, ...payload },
  });
  const initialEvents = [
    event('scroll-stability-user', 'MESSAGE', {
      source: 'user', parent_id: '__root__',
      content: `检查运行中的滚动稳定性。\n\n${'用于形成长会话视口的内容。 '.repeat(1_000)}`,
    }),
    event('scroll-stability-thought', 'THOUGHT', {
      source: 'agent', parent_id: 'scroll-stability-user', content: '初始思考内容', thought: '初始思考内容',
    }),
  ];
  const eventBatch = {
    events: initialEvents,
    next_cursor: 'scroll-stability-thought',
    history_cursor: "older-1",
    result: { status: 'RUNNING' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', route => { stream = route; });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, user)
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) return json(route, {
      events: eventBatch,
      context: { model_name: 'scroll-model', window_tokens: 128_000, used_tokens: 2_048, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) {
      const cursor = new URL(request.url()).searchParams.get('history_cursor');
      if (!cursor) return json(route, eventBatch);
      historyRequests.push({ cursor, at: Date.now() });
      const number = Number(cursor.split('-')[1]);
      return json(route, {
        events: [event(cursor, 'MESSAGE', { source: 'user', content: `历史消息 ${number}`, parent_id: number < 3 ? `older-${number + 1}` : '__root__' })],
        next_cursor: eventBatch.next_cursor,
        history_cursor: number < 3 ? `older-${number + 1}` : null,
      });
    }
    if (path.endsWith('/conversation-activity')) return json(route, {
      running_binding_ids: [], condensing_binding_ids: [], condensation_failed_binding_ids: [],
      possibly_stuck_binding_ids: [], failed_binding_ids: [],
    });
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'scroll-model', window_tokens: 128_000, used_tokens: 2_048, usage_current: true });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) {
      workspaceRequests += 1;
      return json(route, { error: { code: 'RUNTIME_AUXILIARY_SATURATED', message: 'Workspace operations are busy; retry shortly' } }, 503);
    }
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/scroll-stability-conversation');
  await expect(page.getByText('初始思考内容')).toBeVisible();
  await expect.poll(() => Boolean(stream)).toBe(true);

  await expect.poll(() => historyRequests.length).toBe(1);
  const workspaceCount = workspaceRequests;
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(3_500);
  expect(historyRequests.map(item => item.cursor)).toEqual(['older-1']);
  expect(workspaceRequests).toBe(workspaceCount);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect.poll(() => historyRequests.length).toBe(3);
  expect(historyRequests.map(item => item.cursor)).toEqual(['older-1', 'older-2', 'older-3']);
  expect(historyRequests[2].at - historyRequests[1].at).toBeGreaterThanOrEqual(1_400);
  // An exhausted native cursor must not restart after the memory lease or focus refresh.
  await page.clock.fastForward(6 * 60_000);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForTimeout(2_000);
  expect(historyRequests).toHaveLength(3);
  expect(workspaceRequests).toBe(workspaceCount);
  // A window moving into already-prefetched formal identities needs no rescan.
  eventBatch.history_cursor = 'older-2';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForTimeout(2_000);
  expect(historyRequests).toHaveLength(3);
  // A genuinely new latest-window history entry remains loadable.
  eventBatch.history_cursor = 'older-4';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => historyRequests.map(item => item.cursor)).toEqual([
    'older-1', 'older-2', 'older-3', 'older-4',
  ]);
});
