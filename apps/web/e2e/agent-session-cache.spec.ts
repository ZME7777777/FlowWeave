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

test('Agent session reuses a validated in-tab branch and refreshes through a bounded shell', async ({ page }) => {
  let authenticated = false;
  let hydrationRequests = 0;
  let headRequests = 0;
  const workspace = {
    id: 'cache-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = ['cache-conversation-a', 'cache-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '缓存会话 A' : '缓存会话 B', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  }));
  const completeEvents = (id: string) => ({
    events: [
      { id: `${id}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `问题 ${id}`, timestamp: now } },
      { id: `${id}-tool`, event_type: 'TOOL_RESULT', payload: { parent_id: `${id}-user`, content: 'x'.repeat(20_000), details: { stdout: 'x'.repeat(20_000) }, timestamp: now } },
      { id: `${id}-assistant`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${id}-tool`, content: `完整回复 ${id}`, timestamp: now } },
    ],
    next_cursor: `${id}-head`,
    history_cursor: null,
    result: { status: 'COMPLETED' },
  });

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, user)
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/hydration')) {
      hydrationRequests += 1;
      const id = path.split('/').at(-2)!;
      return json(route, {
        events: completeEvents(id),
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true, compaction_policy_current: true },
        readiness: { ready: true, execution_status: 'idle' },
      });
    }
    if (path.endsWith('/head')) {
      headRequests += 1;
      const id = path.split('/').at(-2)!;
      return json(route, { cursor: `${id}-head` });
    }
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  // Authenticate outside the workbench, then enter the deep link. This keeps
  // the initial hydration scoped to the conversation under test.
  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/cache-conversation-a');
  await expect(page.getByText('完整回复 cache-conversation-a', { exact: true })).toBeVisible();
  await expect.poll(() => hydrationRequests).toBe(1);
  expect(headRequests).toBe(0);

  await page.getByRole('button', { name: '缓存会话 B', exact: true }).click();
  await expect(page.getByText('完整回复 cache-conversation-b', { exact: true })).toBeVisible();
  await expect.poll(() => hydrationRequests).toBe(2);

  await page.getByRole('button', { name: '缓存会话 A', exact: true }).click();
  await expect(page.getByText('完整回复 cache-conversation-a', { exact: true })).toBeVisible();
  await expect.poll(() => headRequests).toBe(1);
  expect(hydrationRequests).toBe(2);

  await page.reload();
  // The shell contains the latest formal message but excludes the large tool
  // result; it appears before the fresh complete hydration resolves.
  await expect(page.getByText('完整回复 cache-conversation-a', { exact: true })).toBeVisible();
  await expect.poll(() => hydrationRequests).toBe(3);
  await expect.poll(() => page.evaluate(() => {
    const values = Object.entries(sessionStorage).filter(([key]) => key.startsWith('flowweave:agent-session-shell.v1:'));
    return values.length > 0
      && values.every(([, value]) => !value.includes('stdout'))
      && values.every(([, value]) => !value.includes('x'.repeat(20_000)));
  })).toBe(true);
});
