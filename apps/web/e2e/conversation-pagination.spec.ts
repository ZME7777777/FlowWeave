import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-09-30T09:30:00Z';
const user = {
  id: '00000000-0000-0000-0000-000000000030',
  username: 'conversation-pagination-user',
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

function conversation(index: number) {
  return {
    id: `root-pagination-${index}`,
    display_title: `根分页会话 ${index}`,
    title_state: 'MANUAL',
    lifecycle: 'ACTIVE',
    streaming_callback_ready: true,
    write_available: true,
    execution_status: 'idle',
    created_at: now,
    updated_at: now,
  };
}

test('conversation expansion retains the advanced cursor after the initial-page refresh', async ({ page }) => {
  let authenticated = false;
  const requestedCursors: Array<string | null> = [];
  const workspace = {
    id: 'conversation-pagination-workspace', display_name: '会话分页工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const pages = new Map<string, { items: ReturnType<typeof conversation>[]; next_cursor: string | null }>([
    ['', { items: [conversation(1), conversation(2), conversation(3)], next_cursor: 'after-3' }],
    ['after-3', { items: [conversation(4), conversation(5), conversation(6)], next_cursor: 'after-6' }],
    ['after-6', { items: [conversation(7), conversation(8), conversation(9)], next_cursor: null }],
  ]);

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, user)
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      const cursor = url.searchParams.get('cursor');
      requestedCursors.push(cursor);
      return json(route, pages.get(cursor ?? '') ?? { items: [], next_cursor: null });
    }
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'pagination-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/conversation-activity')) return json(route, {
      running_binding_ids: [], condensing_binding_ids: [], condensation_failed_binding_ids: [],
      possibly_stuck_binding_ids: [], failed_binding_ids: [],
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {},
      ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation(1));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/root-pagination-1');
  await expect(page.getByText('根分页会话 3', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '展开显示' }).click();
  await expect(page.getByText('根分页会话 6', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '展开显示' }).click();
  await expect(page.getByText('根分页会话 9', { exact: true })).toBeVisible();

  // The initial query can be read more than once while the route finishes
  // hydrating. Only cursor-bearing requests represent manual pagination.
  expect(requestedCursors.filter((cursor): cursor is string => cursor !== null)).toEqual(['after-3', 'after-6']);
  await expect(page.getByRole('button', { name: '展开显示' })).toHaveCount(0);
});
