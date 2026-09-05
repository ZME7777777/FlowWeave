import { expect, test, type Page, type Route } from '@playwright/test';

type MockUser = {
  id: string;
  username: 'flowweave' | 'user';
  role: 'SUPER_ADMIN' | 'USER';
  is_super_admin: boolean;
};

const admin: MockUser = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'flowweave',
  role: 'SUPER_ADMIN',
  is_super_admin: true,
};
const ordinaryUser: MockUser = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'user',
  role: 'USER',
  is_super_admin: false,
};
const now = '2026-09-05T09:30:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function login(page: Page, username: MockUser['username']) {
  await page.getByLabel('账号').fill(username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByRole('button', { name: '账户与设置' })).toContainText(username);
}

async function logout(page: Page) {
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录 FlowWeave' })).toBeVisible();
}

test('switching accounts immediately replaces isolated Agent session state', async ({ page }) => {
  let currentUser: MockUser | null = null;
  const conversationRequests: string[] = [];

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path.endsWith('/auth/me')) {
      if (currentUser) return json(route, currentUser);
      return json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    }
    if (path.endsWith('/auth/login') && request.method() === 'POST') {
      const payload = request.postDataJSON() as { username: MockUser['username'] };
      currentUser = payload.username === 'flowweave' ? admin : ordinaryUser;
      return json(route, currentUser);
    }
    if (path.endsWith('/auth/logout') && request.method() === 'POST') {
      currentUser = null;
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith('/agent-workspaces/default')) {
      return json(route, {
        id: 'shared-default-workspace',
        display_name: 'Agent 工作区',
        desired_state: 'RUNNING',
        updated_at: now,
      });
    }
    if (path.endsWith('/runtime')) {
      return json(route, { state: 'ACTIVE', write_available: true, message: null, updated_at: now });
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      conversationRequests.push(currentUser?.username ?? 'anonymous');
      return json(route, currentUser?.username === 'flowweave' ? [{
        id: 'admin-conversation',
        display_title: '管理员专属会话',
        title_state: 'MANUAL',
        lifecycle: 'ACTIVE',
        streaming_callback_ready: true,
        created_at: now,
        updated_at: now,
      }] : []);
    }
    if (path.endsWith('/work-directories')) {
      return json(route, {
        root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' },
        items: [],
      });
    }
    if (path.endsWith('/events')) {
      return json(route, { events: [], next_cursor: null });
    }
    if (path.endsWith('/context')) {
      return json(route, { model_name: null, reasoning_effort: null });
    }
    if (path.endsWith('/pending-confirmation')) {
      return json(route, { pending: false });
    }
    if (path.endsWith('/input-readiness')) {
      return json(route, { ready: true });
    }
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) {
      return json(route, []);
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/agent');
  await login(page, 'user');
  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.getByText('管理员专属会话', { exact: true })).toHaveCount(0);
  await expect.poll(() => conversationRequests).toEqual(['user']);

  await logout(page);
  await login(page, 'flowweave');
  await expect(page.getByRole('button', { name: '管理员专属会话 可继续会话' })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/conversations\/admin-conversation$/);
  await expect.poll(() => conversationRequests).toEqual(['user', 'flowweave']);

  await page.evaluate(() => {
    sessionStorage.setItem('flowweave.agent.conversation-draft.v1', '{"content":"admin draft"}');
    sessionStorage.setItem(
      'flowweave:agent-session-tools:agent-workspace:shared-default-workspace',
      '{"admin-only-scope":{"tabs":[{"kind":"files","id":"files"}]}}',
    );
  });
  await logout(page);
  await expect(page).toHaveURL(/\/agent$/);
  await login(page, 'user');

  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.getByText('管理员专属会话', { exact: true })).toHaveCount(0);
  await expect.poll(() => conversationRequests).toEqual(['user', 'flowweave', 'user']);
  await expect.poll(() => page.evaluate(() => {
    const tools = sessionStorage.getItem('flowweave:agent-session-tools:agent-workspace:shared-default-workspace');
    return {
      draft: sessionStorage.getItem('flowweave.agent.conversation-draft.v1'),
      hasAdminTools: tools?.includes('admin-only-scope') ?? false,
    };
  })).toEqual({ draft: null, hasAdminTools: false });
});
