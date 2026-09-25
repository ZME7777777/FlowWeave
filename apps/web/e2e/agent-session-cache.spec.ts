import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-09-12T09:30:00Z';
const nestedMarkdownSource = [
  '```markdown',
  '# 内层标题',
  '',
  '```text',
  'inner content',
  '```',
  '',
  '外层源码的后续内容',
  '```',
].join('\n');
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

test('Agent session hydrates the first screen without parallel Runtime snapshot reads', async ({ page }) => {
  let authenticated = false;
  let hydrationReads = 0;
  let fallbackReads = 0;
  const workspace = {
    id: 'hydration-workspace', display_name: '首屏水合工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'hydration-conversation', display_title: '首屏水合会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };
  const hydration = {
    events: {
      events: [
        { id: 'hydration-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '读取首屏状态', timestamp: now } },
        { id: 'hydration-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'hydration-user', content: '已由 hydration 返回完整首屏。', timestamp: now } },
      ],
      next_cursor: 'hydration-agent', history_cursor: null, result: { status: 'COMPLETED' },
    },
    context: { model_name: 'hydration-model', window_tokens: 128_000, used_tokens: 2_048, usage_current: true },
    readiness: { ready: true, execution_status: 'idle' },
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) { hydrationReads += 1; return json(route, hydration); }
    if (path.endsWith('/events') || path.endsWith('/input-readiness') || path.endsWith('/context')) {
      fallbackReads += 1;
      return json(route, { error: { code: 'UNEXPECTED_FALLBACK', message: '首屏不应并发读取独立 Runtime 快照' } }, 500);
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/hydration-conversation');
  await expect(page.getByText('已由 hydration 返回完整首屏。')).toBeVisible();
  await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
  expect(hydrationReads).toBe(1);
  expect(fallbackReads).toBe(0);
});


test('Completed conversation shows an explicit loading state without appearing to think', async ({ page }) => {
  let authenticated = false;
  let releaseHydration: (() => void) | undefined;
  const hydrationGate = new Promise<void>(resolve => { releaseHydration = resolve; });
  const workspace = {
    id: 'completed-loading-workspace', display_name: '历史会话工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'completed-loading-conversation', display_title: '已结束会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'unknown',
    created_at: now, updated_at: now,
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) {
      await hydrationGate;
      return json(route, {
        events: {
          events: [
            { id: 'completed-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '历史问题', timestamp: now } },
            { id: 'completed-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'completed-user', content: '历史回复', timestamp: now } },
          ],
          next_cursor: 'completed-agent', history_cursor: null, result: { status: 'COMPLETED' },
        },
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
        readiness: { ready: true, execution_status: 'idle' },
      });
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/completed-loading-conversation');
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toBeVisible();
  await expect(page.locator('.conversation-turn-status')).toHaveCount(0);
  await expect(page.getByText('正在思考', { exact: true })).toHaveCount(0);

  releaseHydration?.();
  await expect(page.getByText('历史回复', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
  await expect(page.locator('.conversation-turn-status')).toHaveCount(0);
});


test('Re-entering a recently loaded conversation reuses its trusted snapshot', async ({ page }) => {
  let authenticated = false;
  let conversationAHydrationReads = 0;
  const workspace = {
    id: 'atomic-loading-workspace', display_name: '原子加载工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = [
    {
      id: 'atomic-loading-a', display_title: '原子加载会话 A', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: now, updated_at: now,
    },
    {
      id: 'atomic-loading-b', display_title: '原子加载会话 B', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: now, updated_at: now,
    },
  ];
  const hydration = (bindingId: string, content: string) => ({
    events: {
      events: [
        { id: `${bindingId}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `问题 ${bindingId}`, timestamp: now } },
        ...(content ? [{ id: `${bindingId}-agent`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${bindingId}-user`, content, timestamp: now } }] : []),
      ],
      next_cursor: content ? `${bindingId}-agent` : `${bindingId}-user`,
      history_cursor: null,
      result: { status: content ? 'COMPLETED' : 'RUNNING' },
    },
    context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
    readiness: { ready: Boolean(content), execution_status: content ? 'idle' : 'running' },
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
      const bindingId = path.split('/').at(-2)!;
      if (bindingId === 'atomic-loading-a') {
        conversationAHydrationReads += 1;
        return json(route, hydration(bindingId, '会话 A 的历史回复'));
      }
      return json(route, hydration(bindingId, '会话 B 的回复'));
    }
    if (path.endsWith('/events') || path.endsWith('/input-readiness') || path.endsWith('/context')) {
      return json(route, { error: { code: 'UNEXPECTED_FALLBACK', message: 'hydration 成功后不应读取首屏回退接口' } }, 500);
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/atomic-loading-a');
  await expect(page.getByText('会话 A 的历史回复', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '原子加载会话 B', exact: true }).click();
  await expect(page.getByText('会话 B 的回复', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '原子加载会话 A', exact: true }).click();

  await expect(page.getByText('会话 A 的历史回复', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(conversationAHydrationReads).toBe(1);
});


test('Re-entering a recently loaded running conversation keeps progress visible and reconciles in background', async ({ page }) => {
  let authenticated = false;
  let hydrationReads = 0;
  let eventReads = 0;
  const workspace = {
    id: 'running-hot-cache-workspace', display_name: '运行中热缓存工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = [
    {
      id: 'running-hot-a', display_title: '运行中热缓存 A', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running',
      created_at: now, updated_at: now,
    },
    {
      id: 'running-hot-b', display_title: '运行中热缓存 B', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: now, updated_at: now,
    },
  ];
  const hydration = (bindingId: string, running: boolean) => ({
    events: {
      events: running ? [
        { id: `${bindingId}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '运行中的问题', timestamp: now } },
        { id: `${bindingId}-thought`, event_type: 'THOUGHT', payload: { source: 'agent', parent_id: `${bindingId}-user`, content: '已加载的最新进度', thought: '已加载的最新进度', timestamp: now } },
      ] : [
        { id: `${bindingId}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '历史问题', timestamp: now } },
        { id: `${bindingId}-agent`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${bindingId}-user`, content: '历史回复', timestamp: now } },
      ],
      next_cursor: running ? `${bindingId}-thought` : `${bindingId}-agent`,
      history_cursor: null,
      result: { status: running ? 'RUNNING' : 'COMPLETED' },
    },
    context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
    readiness: { ready: !running, execution_status: running ? 'running' : 'idle' },
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
      hydrationReads += 1;
      const bindingId = path.split('/').at(-2)!;
      return json(route, hydration(bindingId, bindingId === 'running-hot-a'));
    }
    if (path.endsWith('/events')) {
      eventReads += 1;
      return json(route, hydration('running-hot-a', true).events);
    }
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, hydration('running-hot-a', true).context);
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/running-hot-a');
  await expect(page.getByText('已加载的最新进度', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '运行中热缓存 B', exact: true }).click();
  await expect(page.getByText('历史回复', { exact: true })).toBeVisible();
  const readsBeforeReturn = eventReads;

  await page.getByRole('button', { name: '运行中热缓存 A', exact: true }).click();
  await expect(page.getByText('已加载的最新进度', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
  expect(hydrationReads).toBe(2);
  await expect.poll(() => eventReads).toBeGreaterThan(readsBeforeReturn);
});

test('Rapid conversation switching hydrates only the settled selection', async ({ page }) => {
  let authenticated = false;
  const hydrationReads: string[] = [];
  let releaseFirstHydration: (() => void) | undefined;
  const firstHydrationGate = new Promise<void>(resolve => { releaseFirstHydration = resolve; });
  const workspace = {
    id: 'rapid-switch-workspace', display_name: '快速切换工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = ['rapid-switch-a', 'rapid-switch-b', 'rapid-switch-c'].map((id, index) => ({
    id, display_title: `快速切换会话 ${String.fromCharCode(65 + index)}`, title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  }));

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
      const bindingId = path.split('/').at(-2)!;
      hydrationReads.push(bindingId);
      if (bindingId === 'rapid-switch-a') await firstHydrationGate;
      return json(route, {
        events: {
          events: [
            { id: `${bindingId}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `问题 ${bindingId}`, timestamp: now } },
            { id: `${bindingId}-agent`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${bindingId}-user`, content: `回复 ${bindingId}`, timestamp: now } },
          ],
          next_cursor: `${bindingId}-agent`, history_cursor: null, result: { status: 'COMPLETED' },
        },
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
        readiness: { ready: true, execution_status: 'idle' },
      });
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/rapid-switch-a');
  await expect.poll(() => hydrationReads).toEqual(['rapid-switch-a']);

  await page.getByRole('button', { name: '快速切换会话 B', exact: true }).click();
  await page.getByRole('button', { name: '快速切换会话 C', exact: true }).click();
  await page.waitForTimeout(200);
  expect(hydrationReads).toEqual(['rapid-switch-a']);

  releaseFirstHydration?.();
  await expect(page.getByText('回复 rapid-switch-c', { exact: true })).toBeVisible();
  expect(hydrationReads).toEqual(['rapid-switch-a', 'rapid-switch-c']);
});


test('Agent session restores independent Runtime reads when hydration is unavailable', async ({ page }) => {
  let authenticated = false;
  let hydrationReads = 0;
  let fallbackReads = 0;
  const workspace = {
    id: 'hydration-fallback-workspace', display_name: '水合回退工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'hydration-fallback-conversation', display_title: '水合回退会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) {
      hydrationReads += 1;
      return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: '旧 Runtime 不提供 hydration' } }, 404);
    }
    if (path.endsWith('/events')) {
      fallbackReads += 1;
      return json(route, {
        events: [{ id: 'hydration-fallback-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: '已安全回退到独立读取。', timestamp: now } }],
        next_cursor: 'hydration-fallback-agent', history_cursor: null, result: { status: 'COMPLETED' },
      });
    }
    if (path.endsWith('/input-readiness')) { fallbackReads += 1; return json(route, { ready: true, execution_status: 'idle' }); }
    if (path.endsWith('/context')) { fallbackReads += 1; return json(route, { model_name: 'fallback-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true }); }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/hydration-fallback-conversation');
  await expect(page.getByText('已安全回退到独立读取。')).toBeVisible();
  await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
  expect(hydrationReads).toBe(1);
  expect(fallbackReads).toBe(3);
});

test('Agent session renders a completed long Markdown reply without manual expansion', async ({ page }) => {
  let authenticated = false;
  let eventRequests = 0;
  const workspace = {
    id: 'cache-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = ['cache-conversation-a', 'cache-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '缓存会话 A' : '缓存会话 B', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  }));
  const completeEvents = (id: string) => ({
    events: [
      { id: `${id}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `问题 ${id}\n第二行 ${id}`, timestamp: now } },
      { id: `${id}-tool`, event_type: 'TOOL_RESULT', payload: { parent_id: `${id}-user`, content: 'x'.repeat(20_000), details: { stdout: 'x'.repeat(20_000) }, timestamp: now } },
      { id: `${id}-assistant`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${id}-tool`, content: `完整回复 ${id}\n\n| 选择 | 项目 | 用途 | Git 地址 |\n| --- | --- | --- | --- |\n| #1 | \`hq-support\` | 同步 Kafka topic | \`https://gitlab.example.test/hq-support\` |\n\n${'完整 Markdown 内容 '.repeat(500)}\n\n${nestedMarkdownSource}`, timestamp: now } },
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
    if (path.endsWith('/events')) {
      eventRequests += 1;
      const id = path.split('/').at(-2)!;
      return json(route, completeEvents(id));
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
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  // Authenticate outside the workbench, then enter the deep link. This keeps
  // the initial native event read scoped to the conversation under test.
  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/cache-conversation-a');
  await expect(page.getByText('完整回复 cache-conversation-a', { exact: false })).toBeVisible();
  await expect(page.locator('.conversation-message.user .conversation-message-content')).toHaveCSS('white-space', 'pre-wrap');
  await expect(page.locator('.conversation-message.user .conversation-message-content')).toHaveText('问题 cache-conversation-a\n第二行 cache-conversation-a');
  await expect(page.getByText('完整 Markdown 内容', { exact: false })).toBeVisible();
  const table = page.locator('.conversation-markdown-table-scroll table');
  await expect(table).toBeVisible();
  await expect(table.getByRole('columnheader', { name: '选择' })).toHaveCSS('white-space', 'nowrap');
  await expect(table.locator('td').first()).toHaveCSS('white-space', 'nowrap');
  await expect(page.locator('.conversation-markdown-table-scroll')).toHaveCSS('overflow-x', 'auto');
  const nestedSourceBlock = page.locator('.conversation-code-block').filter({ hasText: '外层源码的后续内容' });
  await expect(nestedSourceBlock).toHaveCount(1);
  await expect(nestedSourceBlock.locator('pre')).toContainText('# 内层标题');
  await expect(nestedSourceBlock.locator('pre')).toContainText('```text');
  await expect(nestedSourceBlock.locator('pre')).toContainText('外层源码的后续内容');
  await expect(page.getByRole('button', { name: '渲染完整消息' })).toHaveCount(0);
  const completedReply = page.locator('.conversation-message.assistant').filter({ hasText: '完整回复 cache-conversation-a' });
  const completedSurface = page.locator('.conversation-surface');
  await completedReply.evaluate(element => { (element as HTMLElement).dataset.focusStabilityMarker = 'stable'; });
  const completedReplyRect = await completedReply.evaluate(element => element.getBoundingClientRect().toJSON());
  const completedScrollTop = await completedSurface.evaluate(element => element.scrollTop);
  const completedEventRequests = eventRequests;
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(completedReply).toHaveAttribute('data-focus-stability-marker', 'stable');
  await expect.poll(() => completedReply.evaluate(element => element.getBoundingClientRect().toJSON())).toEqual(completedReplyRect);
  await expect.poll(() => completedSurface.evaluate(element => element.scrollTop)).toBe(completedScrollTop);
  expect(eventRequests).toBe(completedEventRequests);
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(composer).toBeEditable();
  await page.getByRole('button', { name: '缓存会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/cache-conversation-b$/);
  await expect(composer).toBeEditable();
  await expect.poll(() => eventRequests).toBe(2);
});

test('Deleting the selected conversation immediately hides it and opens its neighbour', async ({ page }) => {
  let authenticated = false;
  let deleteRequested = false;
  let deleted = false;
  let releaseDelete: (() => void) | undefined;
  const deleteGate = new Promise<void>(resolve => { releaseDelete = resolve; });
  const workspace = {
    id: 'optimistic-delete-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = ['optimistic-delete-a', 'optimistic-delete-b'].map((id, index) => ({
    id, display_title: index === 0 ? '待删除会话' : '下一会话', title_state: 'MANUAL' as const,
    lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: index === 0 ? '2026-09-12T09:31:00Z' : '2026-09-12T09:30:00Z',
    updated_at: now,
  }));

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
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      return json(route, { items: deleted ? conversations.filter(item => item.id !== 'optimistic-delete-a') : conversations, next_cursor: null });
    }
    if (path.endsWith('/conversations/optimistic-delete-a') && request.method() === 'DELETE') {
      deleteRequested = true;
      await deleteGate;
      deleted = true;
      return json(route, {});
    }
    if (path.endsWith('/events')) {
      const id = path.split('/').at(-2)!;
      return json(route, { events: [], next_cursor: `${id}-head`, history_cursor: null, result: { status: 'COMPLETED' } });
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
    if (path.endsWith('/context')) return json(route, { model_name: 'delete-model', window_tokens: 128_000, used_tokens: 0, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/optimistic-delete-a');
  await expect(page.locator('h2.agent-session-title')).toHaveText('待删除会话');
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();

  // The delayed API response must not keep the deleted row or route on
  // screen. The next existing session becomes visible in the same frame.
  await expect.poll(() => deleteRequested).toBe(true);
  await expect(page).toHaveURL(/\/agent\/conversations\/optimistic-delete-b$/);
  await expect(page.locator('h2.agent-session-title')).toHaveText('下一会话');
  await expect(page.getByRole('button', { name: '待删除会话', exact: true })).toHaveCount(0);
  await expect(page.getByText('开始一个新的会话', { exact: true })).toHaveCount(0);

  releaseDelete?.();
});

test('Existing conversation composers keep text and attachments isolated', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'composer-scope-workspace', display_name: '输入隔离工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['composer-scope-a', 'composer-scope-b'].map((id, index) => ({
    id, display_title: `输入会话 ${String.fromCharCode(65 + index)}`, title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  }));

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/attachments') && request.method() === 'POST') {
      const bindingId = path.split('/').at(-2)!;
      return json(route, {
        filename: `${bindingId}.txt`, mime_type: 'text/plain', byte_size: 8,
        path: `/runtime/workspace/project/uploads/${bindingId}.txt`,
      });
    }
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/composer-scope-a');
  const composer = page.getByLabel('发送 Agent 消息');
  const attachment = (name: string) => page.locator('.agent-composer .agent-attachments').getByText(name, { exact: true });

  await composer.fill('只属于会话 A 的草稿');
  await page.getByLabel('上传附件').setInputFiles({ name: 'composer-scope-a.txt', mimeType: 'text/plain', buffer: Buffer.from('scope-a') });
  await expect(attachment('composer-scope-a.txt')).toBeVisible();

  await page.getByRole('button', { name: '输入会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/composer-scope-b$/);
  await expect(composer).toHaveValue('');
  await expect(page.locator('.agent-composer .agent-attachments')).toHaveCount(0);

  await composer.fill('只属于会话 B 的草稿');
  await page.getByLabel('上传附件').setInputFiles({ name: 'composer-scope-b.txt', mimeType: 'text/plain', buffer: Buffer.from('scope-b') });
  await expect(attachment('composer-scope-b.txt')).toBeVisible();

  await page.getByRole('button', { name: '输入会话 A', exact: true }).click();
  await expect(composer).toHaveValue('只属于会话 A 的草稿');
  await expect(attachment('composer-scope-a.txt')).toBeVisible();
  await expect(attachment('composer-scope-b.txt')).toHaveCount(0);

  await page.getByRole('button', { name: '输入会话 B', exact: true }).click();
  await expect(composer).toHaveValue('只属于会话 B 的草稿');
  await expect(attachment('composer-scope-b.txt')).toBeVisible();
  await expect(attachment('composer-scope-a.txt')).toHaveCount(0);
});

test('Conversation attachments open in a preview dialog before the file sidebar', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'attachment-preview-workspace', display_name: '附件预览工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'attachment-preview-conversation', display_title: '附件预览会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };
  const attachment = {
    filename: '需求说明.txt', mime_type: 'text/plain', byte_size: 24,
    path: '/runtime/workspace/project/uploads/requirements.txt',
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/events')) return json(route, {
      events: [{ id: 'attachment-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请查看附件', attachments: [attachment], timestamp: now } }],
      next_cursor: 'attachment-user', history_cursor: null, result: { status: 'COMPLETED' },
    });
    if (path.endsWith('/file') && url.searchParams.get('preview') === 'true') return route.fulfill({ status: 200, contentType: 'text/plain', headers: { 'X-Preview-Total-Bytes': '36' }, body: '弹窗内可直接阅读附件内容' });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [{ kind: 'file', path: attachment.path, name: attachment.filename, size: attachment.byte_size }], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/attachment-preview-conversation');
  await page.getByRole('button', { name: /需求说明\.txt/ }).click();

  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview).toBeVisible();
  await expect(preview.getByText('弹窗内可直接阅读附件内容')).toBeVisible();
  await expect(page.locator('.agent-workspace-drawer')).not.toHaveClass(/tools-open/);

  await preview.getByRole('button', { name: '全屏预览' }).click();
  await expect(preview).toHaveClass(/fullscreen/);
  await page.keyboard.press('Escape');
  await expect(preview).toBeHidden();

  await page.getByRole('button', { name: /需求说明\.txt/ }).click();
  await expect(preview).toBeVisible();
  await preview.getByRole('button', { name: '在文件栏打开' }).click();
  await expect(preview).toBeHidden();
  await expect(page.locator('.agent-workspace-drawer')).toHaveClass(/tools-open/);
});



test('A delayed message response never renders in another conversation', async ({ page }) => {
  let authenticated = false;
  let releaseSend: (() => void) | undefined;
  let sentBindingId: string | undefined;
  const sentMessages: string[] = [];
  const sendGate = new Promise<void>(resolve => { releaseSend = resolve; });
  const workspace = {
    id: 'send-switch-workspace', display_name: '消息隔离工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = ['send-switch-a', 'send-switch-b'].map((id, index) => ({
    id, display_title: index === 0 ? '发送会话 A' : '发送会话 B', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  }));

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
    if (path.endsWith('/messages') && request.method() === 'POST') {
      const bindingId = path.split('/').at(-2)!;
      sentBindingId ??= bindingId;
      sentMessages.push(bindingId);
      if (bindingId === 'send-switch-a') await sendGate;
      return json(route, { accepted: true, cursor: `${bindingId}-user-sent` });
    }
    if (path.endsWith('/events')) {
      const id = path.split('/').at(-2)!;
      return json(route, {
        events: [{ id: `${id}-initial`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: `初始消息 ${id}`, timestamp: now } }],
        next_cursor: `${id}-initial`, history_cursor: null, result: { status: 'COMPLETED' },
      });
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
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/send-switch-a');
  await expect(page.getByText('初始消息 send-switch-a')).toBeVisible();

  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('只属于会话 A 的消息');
  await page.getByLabel('发送消息').click();
  await expect(page.locator('.conversation-message.user').filter({ hasText: '只属于会话 A 的消息' })).toBeVisible();
  await expect.poll(() => sentBindingId).toBe('send-switch-a');

  await page.getByRole('button', { name: '发送会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/send-switch-b$/);
  await expect(page.getByText('初始消息 send-switch-b')).toBeVisible();
  await expect(page.getByText('只属于会话 A 的消息')).toHaveCount(0);
  await composer.fill('会话 B 正在编辑的草稿');
  await page.getByLabel('发送消息').click();
  await expect.poll(() => sentMessages).toEqual(['send-switch-a', 'send-switch-b']);
  await expect(page.locator('.conversation-message.user').filter({ hasText: '会话 B 正在编辑的草稿' })).toBeVisible();

  releaseSend?.();
  await expect.poll(() => page.getByText('只属于会话 A 的消息').count()).toBe(0);
  await expect(page.locator('.conversation-message.user').filter({ hasText: '会话 B 正在编辑的草稿' })).toBeVisible();
  await expect(page.getByText('初始消息 send-switch-b')).toBeVisible();
});

test('Agent transcript keeps scroll ownership through streamed output and historical paging', async ({ page }) => {
  let authenticated = false;
  let activeEventRequests = 0;
  let historyRequests = 0;
  let thirdHistoryCompleted = false;
  let releaseFirstHistory: (() => void) | undefined;
  let releaseSecondHistory: (() => void) | undefined;
  let releaseThirdHistory: (() => void) | undefined;
  let agentStream: { send(message: string): void } | undefined;
  const firstHistoryGate = new Promise<void>(resolve => { releaseFirstHistory = resolve; });
  const secondHistoryGate = new Promise<void>(resolve => { releaseSecondHistory = resolve; });
  const thirdHistoryGate = new Promise<void>(resolve => { releaseThirdHistory = resolve; });
  const workspace = {
    id: 'scroll-workspace', display_name: '滚动回归工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversations = [
    {
      id: 'scroll-conversation-a', display_title: '滚动会话 A', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running',
      created_at: now, updated_at: now,
    },
    {
      id: 'scroll-conversation-b', display_title: '滚动会话 B', title_state: 'MANUAL',
      lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running',
      created_at: now, updated_at: now,
    },
  ];
  const event = (id: string, eventType: string, payload: Record<string, unknown>) => ({
    id, event_type: eventType, payload: { timestamp: now, ...payload },
  });
  const activeEvents = (id: string, refreshMarker = 0) => id === 'scroll-conversation-a' ? {
    events: [
      event('scroll-a-user', 'MESSAGE', {
        source: 'user', parent_id: '__root__',
        content: `开始当前会话。\n\n${'用于制造真实滚动高度的当前会话内容。 '.repeat(1_000)}`,
      }),
      event('scroll-a-thought', 'THOUGHT', {
        source: 'agent', parent_id: 'scroll-a-user', content: '稳定阅读锚点', thought: '稳定阅读锚点',
        // This backend-only field changes on every reconciliation without
        // changing the visible transcript row.
        refresh_marker: refreshMarker,
      }),
    ],
    next_cursor: 'scroll-a-thought',
    history_cursor: 'scroll-history-1',
    result: { status: 'RUNNING' },
  } : {
    events: [
      event('scroll-b-user', 'MESSAGE', {
        source: 'user', parent_id: '__root__',
        content: `会话 B 的最新内容。\n\n${'会话 B 必须不受会话 A 迟到历史分页影响。 '.repeat(1_000)}`,
      }),
      event('scroll-b-thought', 'THOUGHT', {
        source: 'agent', parent_id: 'scroll-b-user', content: '会话 B 稳定锚点', thought: '会话 B 稳定锚点',
      }),
    ],
    next_cursor: 'scroll-b-thought',
    history_cursor: null,
    result: { status: 'RUNNING' },
  };
  const expectAtLatest = async () => {
    const surface = page.locator('.conversation-surface');
    await expect.poll(() => surface.evaluate(element => {
      return element.scrollHeight - element.scrollTop - element.clientHeight;
    })).toBeLessThanOrEqual(16);
    await expect(page.getByRole('button', { name: /跳转到.*最新回复/ })).toHaveCount(0);
  };
  const visibleAnchor = async () => page.locator('.conversation-surface').evaluate(element => {
    const bounds = element.getBoundingClientRect();
    const candidate = document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + Math.min(96, bounds.height / 2))
      ?.closest<HTMLElement>('[data-conversation-event-id], .conversation-activity-row');
    return candidate?.dataset.conversationEventId ?? candidate?.textContent?.trim().slice(0, 120) ?? '';
  });

  await page.routeWebSocket('**/agent-workspaces/**/stream', stream => { agentStream = stream; });
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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/events')) {
      const bindingId = path.split('/').at(-2)!;
      const historyCursor = url.searchParams.get('history_cursor');
      if (bindingId === 'scroll-conversation-a' && historyCursor === 'scroll-history-1') {
        historyRequests += 1;
        await firstHistoryGate;
        return json(route, {
          events: [event('scroll-history-one', 'MESSAGE', {
            source: 'user', parent_id: '__root__',
            content: `第一历史页。\n\n${'第一历史页的内容。 '.repeat(400)}`,
          })],
          next_cursor: null,
          history_cursor: 'scroll-history-2',
        });
      }
      if (bindingId === 'scroll-conversation-a' && historyCursor === 'scroll-history-2') {
        historyRequests += 1;
        await secondHistoryGate;
        return json(route, {
          events: [event('scroll-history-two', 'MESSAGE', {
            source: 'user', parent_id: '__root__',
            content: `第二历史页。\n\n${'第二历史页的内容。 '.repeat(400)}`,
          })],
          next_cursor: null,
          history_cursor: 'scroll-history-3',
        });
      }
      if (bindingId === 'scroll-conversation-a' && historyCursor === 'scroll-history-3') {
        historyRequests += 1;
        await thirdHistoryGate;
        thirdHistoryCompleted = true;
        return json(route, { error: { code: 'HISTORY_UNAVAILABLE', message: '历史页暂时不可用' } }, 503);
      }
      const refreshMarker = bindingId === 'scroll-conversation-a' ? ++activeEventRequests : 0;
      return json(route, activeEvents(bindingId, refreshMarker));
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
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'scroll-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/scroll-conversation-a');
  await expect(page.getByText('稳定阅读锚点')).toBeVisible();
  await expect.poll(() => Boolean(agentStream)).toBe(true);
  await expect.poll(() => historyRequests).toBe(1);
  const surface = page.locator('.conversation-surface');
  await surface.hover();
  await page.mouse.wheel(0, 20_000);
  await expectAtLatest();

  // A native refresh can change fields that have no visual representation.
  // It must not write the transcript's bottom offset merely because its event
  // array was reconciled. Wrap this instance only after its initial alignment.
  await surface.evaluate(element => {
    let prototype: object | null = element;
    let descriptor: PropertyDescriptor | undefined;
    while (prototype && !descriptor) {
      descriptor = Object.getOwnPropertyDescriptor(prototype, 'scrollTop');
      prototype = Object.getPrototypeOf(prototype);
    }
    if (!descriptor?.get || !descriptor.set) throw new Error('scrollTop descriptor is unavailable');
    element.dataset.refreshScrollWrites = '0';
    Object.defineProperty(element, 'scrollTop', {
      configurable: true,
      get: () => descriptor!.get!.call(element),
      set: value => {
        const writes = Number(element.dataset.refreshScrollWrites ?? '0') + 1;
        element.dataset.refreshScrollWrites = String(writes);
        descriptor!.set!.call(element, value);
      },
    });
  });
  await expect.poll(() => activeEventRequests).toBe(2);
  await page.waitForTimeout(750);
  expect(activeEventRequests).toBe(2);
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(surface).toHaveAttribute('data-refresh-scroll-writes', '0');
  await surface.evaluate(element => {
    element.dataset.refreshScrollWrites = '0';
    element.querySelector<HTMLElement>('.conversation-turn-status')?.style.setProperty('min-height', '36px');
  });
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(surface).toHaveAttribute('data-refresh-scroll-writes', '0');
  await surface.evaluate(element => { element.dataset.refreshScrollWrites = '0'; });
  agentStream!.send(JSON.stringify({
    type: 'delta', item_id: 'scroll-live-preview', content: '最终回复应等待正式消息后一次性渲染。',
  }));
  await expect(page.getByLabel('正在生成的回复')).toHaveCount(0);
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(surface).toHaveAttribute('data-refresh-scroll-writes', '0');

  agentStream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-tool-one', 'TOOL_CALL', {
      source: 'agent', parent_id: 'scroll-a-user', action_id: 'scroll-tool-one', tool_call_id: 'scroll-call-one',
      tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'git status --short' },
    }),
  }));
  await expect(page.getByText('正在运行 git status --short')).toBeVisible();
  await expectAtLatest();
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-tool-one-result', 'TOOL_RESULT', {
      source: 'environment', parent_id: 'scroll-tool-one', action_id: 'scroll-tool-one', tool_call_id: 'scroll-call-one',
      tool_name: 'terminal', event_name: 'TerminalObservation', content: 'M ConversationSurface.tsx',
      details: { command: 'git status --short', stdout: 'M ConversationSurface.tsx', exit_code: 0, is_error: false },
    }),
  }));
  const detail = page.locator('.conversation-tool-detail').filter({ hasText: 'git status --short' });
  await expect(detail).toBeVisible();
  await expectAtLatest();
  await detail.locator(':scope > summary').click();
  await expect(detail.getByText('M ConversationSurface.tsx', { exact: true })).toBeVisible();
  await expectAtLatest();
  await detail.locator(':scope > summary').click();
  await expectAtLatest();
  const longFinalReply = Array.from(
    { length: 90 },
    (_, index) => `正式回复第 ${index + 1} 段：这一段用于验证正式 assistant 回复整块插入后仍稳定停在最新内容。`,
  ).join('\n\n');
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-formal-reply', 'MESSAGE', {
      source: 'agent', parent_id: 'scroll-tool-one-result', content: longFinalReply,
    }),
  }));
  await expect(page.getByText('正式回复第 90 段：这一段用于验证正式 assistant 回复整块插入后仍稳定停在最新内容。')).toBeVisible();
  await expect.poll(() => page.locator('.conversation-message.assistant').last().evaluate(reply => {
    const surface = reply.closest<HTMLElement>('.conversation-surface');
    return reply.getBoundingClientRect().height - (surface?.clientHeight ?? 0);
  })).toBeGreaterThan(0);
  await expectAtLatest();

  releaseFirstHistory?.();
  await expect(page.getByText('第一历史页。', { exact: false })).toBeVisible();
  await expectAtLatest();
  await expect.poll(() => historyRequests).toBe(2);

  await surface.hover();
  const bottomBeforeReading = await surface.evaluate(element => element.scrollTop);
  await page.mouse.wheel(0, -360);
  await expect.poll(() => surface.evaluate(element => element.scrollTop)).toBeLessThan(bottomBeforeReading);
  await expect(page.getByRole('button', { name: '跳转到正在生成的最新回复' })).toBeVisible();
  const anchorBeforePrepend = await visibleAnchor();
  const readingPosition = await surface.evaluate(element => element.scrollTop);
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-tool-two', 'TOOL_CALL', {
      source: 'agent', parent_id: 'scroll-tool-one-result', action_id: 'scroll-tool-two', tool_call_id: 'scroll-call-two',
      tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'git diff --check' },
    }),
  }));
  await expect(page.getByText('正在运行 git diff --check')).toBeVisible();
  await expect.poll(() => surface.evaluate(element => element.scrollTop)).toBe(readingPosition);

  releaseSecondHistory?.();
  await expect(page.getByText('第二历史页。', { exact: false })).toBeVisible();
  await expect.poll(visibleAnchor).toBe(anchorBeforePrepend);
  await expect(page.getByRole('button', { name: '跳转到正在生成的最新回复' })).toBeVisible();
  await expect.poll(() => historyRequests).toBe(3);

  await page.getByRole('button', { name: '滚动会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scroll-conversation-b$/);
  await expect(page.getByText('会话 B 稳定锚点')).toBeVisible();
  await expectAtLatest();
  releaseThirdHistory?.();
  await expect.poll(() => thirdHistoryCompleted).toBe(true);
  await expectAtLatest();
});

test('Agent composer retains each conversation draft and uploaded attachment across navigation and reload', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'draft-workspace', display_name: '草稿工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['draft-conversation-a', 'draft-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '草稿会话 A' : '草稿会话 B', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  }));
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/attachments') && request.method() === 'POST') return json(route, {
      filename: '保留附件.txt', mime_type: 'text/plain', byte_size: 7, path: '/runtime/workspace/project/uploads/retained.txt',
    });
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/draft-conversation-a');
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('会话 A 的未发送内容');
  await page.getByLabel('上传附件').setInputFiles({ name: '保留附件.txt', mimeType: 'text/plain', buffer: Buffer.from('retained') });
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '草稿会话 B', exact: true }).click();
  await expect(composer).toHaveValue('');
  await composer.fill('会话 B 的未发送内容');
  await page.getByRole('button', { name: '草稿会话 A', exact: true }).click();
  await expect(composer).toHaveValue('会话 A 的未发送内容');
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toBeVisible();

  // Exercise the fast A -> B -> A path before either debounce timer can
  // settle. The newly selected conversation must not inherit the old draft.
  await page.getByRole('button', { name: '草稿会话 B', exact: true }).click();
  await expect(composer).toHaveValue('会话 B 的未发送内容');
  await page.getByRole('button', { name: '草稿会话 A', exact: true }).click();
  await expect(composer).toHaveValue('会话 A 的未发送内容');

  await page.reload();
  await expect(composer).toHaveValue('会话 A 的未发送内容');
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '草稿会话 B', exact: true }).click();
  await expect(composer).toHaveValue('会话 B 的未发送内容');
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toHaveCount(0);
});


test('New conversation draft remains isolated and can be resumed after switching', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'draft-race-workspace', display_name: '草稿竞态工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['draft-race-a', 'draft-race-b', 'draft-race-c'].map((id, index) => ({
    id, display_title: `竞态会话 ${String.fromCharCode(65 + index)}`, title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  }));
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/attachments') && request.method() === 'POST') return json(route, {
      filename: '新会话附件.txt', mime_type: 'text/plain', byte_size: 9, path: '/runtime/workspace/project/uploads/new-draft.txt',
    });
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent');
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  const composer = page.getByLabel('发送 Agent 消息');
  const draftAttachment = page.locator('.agent-composer .agent-attachments').getByText('新会话附件.txt', { exact: true });
  await composer.fill('只属于新会话的未发送草稿');
  await page.getByLabel('上传附件').setInputFiles({ name: '新会话附件.txt', mimeType: 'text/plain', buffer: Buffer.from('new-draft') });
  await expect(draftAttachment).toBeVisible();

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await page.getByRole('button', { name: '竞态会话 C', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/draft-race-c$/);
  await expect(composer).toHaveValue('');
  await expect(draftAttachment).toHaveCount(0);

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await expect(composer).toHaveValue('');
  await expect(draftAttachment).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => Object.entries(localStorage)
    .filter(([key]) => key.includes('draft-race-workspace:draft-race-'))
    .every(([, value]) => !value.includes('只属于新会话的未发送草稿') && !value.includes('新会话附件.txt')))).toBe(true);

  const recoverDraft = page.getByRole('button', { name: '恢复根工作区的未发送草稿' });
  await recoverDraft.click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(composer).toHaveValue('只属于新会话的未发送草稿');
  await expect(draftAttachment).toBeVisible();

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await page.reload();
  await recoverDraft.click();
  await expect(composer).toHaveValue('只属于新会话的未发送草稿');
  await expect(draftAttachment).toBeVisible();

  await page.getByRole('button', { name: '在根工作区中新建会话' }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(composer).toHaveValue('');
  await expect(draftAttachment).toHaveCount(0);
  await expect(recoverDraft).toHaveCount(0);
});

test('First message keeps the new conversation visible while its routed read is pending', async ({ page }) => {
  let authenticated = false;
  let created = false;
  let releaseConversationRead: (() => void) | undefined;
  const conversationRead = new Promise<void>(resolve => { releaseConversationRead = resolve; });
  const workspace = { id: 'handoff-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'handoff-conversation', display_title: '无闪烁的新会话', title_state: 'PENDING' as const,
    lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, execution_status: 'running',
    model_provider_id: 'handoff-provider', model_name: 'handoff-model', reasoning_effort: null,
    created_at: now, updated_at: now,
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [], next_cursor: null });
    if (path.endsWith('/conversations') && request.method() === 'POST') {
      created = true;
      return json(route, { conversation, accepted: true, cursor: 'handoff-user-event' }, 201);
    }
    if (path.endsWith('/conversations/handoff-conversation') && request.method() === 'GET') {
      await conversationRead;
      return json(route, conversation);
    }
    if (path.endsWith('/events')) return json(route, {
      events: [{ id: 'handoff-user-event', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '首条消息', timestamp: now } }],
      next_cursor: 'handoff-user-event', result: { status: 'RUNNING' },
    });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'handoff-model', window_tokens: 128_000, used_tokens: 0, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, [{
      id: 'handoff-provider', name: '交接模型', connection_state: 'CONNECTED', models: [{ model_name: 'handoff-model', enabled: true, is_default: true }],
    }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(composer).toBeVisible();
  await composer.fill('首条消息');
  await page.getByLabel('发送消息').click();

  await expect.poll(() => created).toBe(true);
  await expect(page).toHaveURL(/\/agent\/conversations\/handoff-conversation$/);
  // The exact-route request remains blocked. The returned create projection
  // must bridge the URL update, rather than briefly rendering the empty state.
  await expect(page.locator('h2.agent-session-title')).toHaveText('无闪烁的新会话');
  await expect(page.locator('.agent-workbench-empty')).toHaveCount(0);

  releaseConversationRead?.();
});


test('A new conversation stays first after an existing conversation was reordered', async ({ page }) => {
  let authenticated = false;
  let created = false;
  let reordered = false;
  const workspace = { id: 'new-order-workspace', display_name: '会话排序工作区', desired_state: 'RUNNING', updated_at: now };
  const existing = [
    {
      id: 'new-order-a', display_title: '已有会话 A', title_state: 'MANUAL' as const,
      lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      model_provider_id: 'new-order-provider', model_name: 'new-order-model', reasoning_effort: null,
      created_at: '2026-09-12T09:20:00Z', updated_at: '2026-09-12T09:20:00Z', sort_key: '2',
    },
    {
      id: 'new-order-b', display_title: '已有会话 B', title_state: 'MANUAL' as const,
      lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      model_provider_id: 'new-order-provider', model_name: 'new-order-model', reasoning_effort: null,
      created_at: '2026-09-12T09:10:00Z', updated_at: '2026-09-12T09:10:00Z', sort_key: '1',
    },
  ];
  const createdConversation = {
    id: 'new-order-created', display_title: '最新会话', title_state: 'PENDING' as const,
    lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'running',
    model_provider_id: 'new-order-provider', model_name: 'new-order-model', reasoning_effort: null,
    created_at: '2026-09-12T09:30:00Z', updated_at: '2026-09-12T09:30:00Z', sort_key: '3',
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      return json(route, { items: created ? [createdConversation, ...existing] : existing, next_cursor: null });
    }
    if (path.endsWith('/conversations') && request.method() === 'POST') {
      created = true;
      return json(route, { conversation: createdConversation, accepted: true, cursor: 'new-order-user-event' }, 201);
    }
    if (path.endsWith('/order') && request.method() === 'POST') {
      reordered = true;
      return json(route, existing[1]);
    }
    if (path.endsWith('/events')) return json(route, {
      events: [], next_cursor: null, history_cursor: null, result: { status: path.includes('new-order-created') ? 'RUNNING' : 'COMPLETED' },
    });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: !path.includes('new-order-created'), execution_status: path.includes('new-order-created') ? 'running' : 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'new-order-model', window_tokens: 128_000, used_tokens: 0, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, [{
      id: 'new-order-provider', name: '排序测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'new-order-model', enabled: true, is_default: true }],
    }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, id === createdConversation.id ? createdConversation : existing.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  const source = page.getByRole('button', { name: '拖拽排序会话 已有会话 B' });
  const target = page.locator('[data-conversation-binding-id="new-order-a"]');
  const sourceBox = await source.boundingBox();
  const targetBox = await target.boundingBox();
  expect(sourceBox).not.toBeNull();
  expect(targetBox).not.toBeNull();
  await page.mouse.move(sourceBox!.x + sourceBox!.width / 2, sourceBox!.y + sourceBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(targetBox!.x + targetBox!.width / 2, targetBox!.y + 2, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => reordered).toBe(true);

  await page.getByRole('button', { name: '在根工作区中新建会话' }).click();
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('创建最新会话');
  await page.getByLabel('发送消息').click();
  await expect.poll(() => created).toBe(true);

  const rootRows = page.locator('.agent-workspace-group').filter({ hasText: '根工作区' }).locator('[data-conversation-binding-id]');
  await expect(rootRows).toHaveCount(3);
  await expect.poll(() => rootRows.evaluateAll(rows => rows.map(row => row.getAttribute('data-conversation-binding-id')))).toEqual([
    'new-order-created', 'new-order-b', 'new-order-a',
  ]);
});


test('Background running conversations stay visible without blocking the conversation list', async ({ page }) => {
  let authenticated = false;
  let releaseActivity: (() => void) | undefined;
  const activityGate = new Promise<void>(resolve => { releaseActivity = resolve; });
  const workspace = {
    id: 'background-activity-workspace', display_name: '后台活动工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const selectedConversation = {
    id: 'selected-idle-conversation', display_title: '当前空闲会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'unknown', created_at: now, updated_at: now,
  };
  const backgroundConversation = {
    id: 'background-running-conversation', display_title: '后台运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'unknown', created_at: now, updated_at: now,
  };

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
    if (path.endsWith('/conversation-activity')) {
      await activityGate;
      return json(route, { running_binding_ids: [backgroundConversation.id] });
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [selectedConversation, backgroundConversation], next_cursor: null });
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, selectedConversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/selected-idle-conversation');

  const selectedRow = page.locator('[data-conversation-binding-id="selected-idle-conversation"]');
  const backgroundRow = page.locator('[data-conversation-binding-id="background-running-conversation"]');
  await expect(selectedRow).toBeVisible();
  await expect(backgroundRow).toBeVisible();
  await expect(backgroundRow.locator('.agent-workspace-conversation-running')).toHaveCount(0);

  releaseActivity?.();
  await expect(backgroundRow.locator('.agent-workspace-conversation-running')).toBeVisible();
  await expect(selectedRow.locator('.agent-workspace-conversation-running')).toHaveCount(0);
});


test('Running Agent session reload restores older history pages', async ({ page }) => {
  let authenticated = false;
  let historyRequests = 0;
  const workspace = {
    id: 'running-history-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'running-history-conversation', display_title: '运行中的压缩会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, execution_status: 'running', created_at: now, updated_at: now,
  };

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/events')) {
      if (url.searchParams.get('history_cursor') === 'compressed-history-page') {
        historyRequests += 1;
        return json(route, {
          events: [{ id: 'compressed-history-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '压缩前仍可见的历史会话', timestamp: now } }],
          next_cursor: null, history_cursor: null,
        });
      }
      return json(route, {
        events: [
          { id: 'compressed-history-condensation', event_type: 'CONDENSATION', payload: { parent_id: 'live-user', summary: '早期会话摘要', forgotten_event_ids: ['old-1'], timestamp: now } },
          { id: 'live-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'compressed-history-condensation', content: '当前仍在处理的请求', timestamp: now } },
        ],
        next_cursor: 'live-user', history_cursor: 'compressed-history-page', result: { status: 'RUNNING' },
      });
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
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/running-history-conversation');
  await expect(page.getByText('当前仍在处理的请求')).toBeVisible();
  await page.reload();
  await expect(page.getByText('当前仍在处理的请求')).toBeVisible();
  await expect.poll(() => historyRequests).toBeGreaterThan(0);
  await expect(page.getByText('压缩前仍可见的历史会话')).toBeVisible();
  const completedHistoryRequests = historyRequests;
  await page.waitForTimeout(4_750);
  expect(historyRequests).toBe(completedHistoryRequests);
});

test('Conversation context menu marks a conversation unread until it is opened again', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'unread-workspace', display_name: '未读工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['unread-conversation-a', 'unread-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '未读会话 A' : '未读会话 B', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', unread: false, created_at: now, updated_at: now,
  }));
  const unreadWrites: Array<{ id: string; unread: boolean }> = [];

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const id = path.split('/').at(-2)!;
      const conversation = conversations.find(item => item.id === id)!;
      const body = request.postDataJSON() as { unread: boolean };
      conversation.unread = body.unread;
      unreadWrites.push({ id, unread: body.unread });
      return json(route, conversation);
    }
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/unread-conversation-a');
  const conversationA = page.getByRole('button', { name: '未读会话 A', exact: true });
  const conversationB = page.getByRole('button', { name: '未读会话 B', exact: true });
  const unreadMarker = conversationA.locator('xpath=..').getByRole('img', { name: '会话已完成，有未读回复' });

  await conversationA.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为未读' }).click();
  await expect(unreadMarker).toBeVisible();
  await expect.poll(() => unreadWrites).toEqual([{ id: 'unread-conversation-a', unread: true }]);

  await page.reload();
  await expect(unreadMarker).toBeVisible();

  await conversationB.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-b$/);
  await expect(unreadMarker).toBeVisible();
  await page.reload();
  await expect(unreadMarker).toBeVisible();

  await conversationA.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-a$/);
  await expect(unreadMarker).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
  ]);
});

test('Opening a conversation stays read when an older list request finishes later', async ({ page }) => {
  let authenticated = false;
  let listReads = 0;
  let staleListDelivered = false;
  const workspace = { id: 'stale-unread-workspace', display_name: '未读竞态工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['stale-unread-conversation-a', 'stale-unread-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '竞态会话 A' : '竞态会话 B', title_state: index === 0 ? 'PENDING' : 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', unread: index === 0, created_at: now, updated_at: now,
  }));

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      listReads += 1;
      const snapshot = structuredClone(conversations);
      if (listReads === 2) {
        await new Promise(resolve => setTimeout(resolve, 2_000));
        staleListDelivered = true;
      }
      return json(route, { items: snapshot, next_cursor: null });
    }
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const id = path.split('/').at(-2)!;
      const conversation = conversations.find(item => item.id === id)!;
      conversation.unread = (request.postDataJSON() as { unread: boolean }).unread;
      await new Promise(resolve => setTimeout(resolve, 2_500));
      return json(route, conversation);
    }
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/stale-unread-conversation-b');
  const conversationA = page.getByRole('button', { name: '竞态会话 A', exact: true });
  const conversationB = page.getByRole('button', { name: '竞态会话 B', exact: true });
  const unreadMarker = conversationA.locator('xpath=..').getByRole('img', { name: '会话已完成，有未读回复' });
  await expect(unreadMarker).toBeVisible();
  await expect.poll(() => listReads).toBeGreaterThanOrEqual(2);

  await conversationA.click();
  await expect(unreadMarker).toHaveCount(0);
  await conversationB.click();
  await expect.poll(() => staleListDelivered).toBe(true);
  await expect(unreadMarker).toHaveCount(0);
});

test('Conversation sidebar pins locally, orders activity, and reveals the selected source row', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'sidebar-workspace', display_name: '侧栏工作区', desired_state: 'RUNNING', updated_at: now };
  const directory = {
    id: 'sidebar-directory', display_name: '归属工作区',
    current_version: { working_directory: '/runtime/workspace/project/directory' },
  };
  const conversations = [
    {
      id: 'sidebar-root-unread', display_title: '未读根会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T09:00:00Z', updated_at: '2026-09-12T09:10:00Z',
    },
    {
      id: 'sidebar-directory-pinned', display_title: '归属工作区会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle', work_directory_id: directory.id,
      created_at: '2026-09-12T09:20:00Z', updated_at: '2026-09-12T09:20:00Z',
    },
    {
      id: 'sidebar-directory-running', display_title: '运行中目标会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'running', work_directory_id: directory.id,
      created_at: '2026-09-12T09:30:00Z', updated_at: '2026-09-12T09:50:00Z',
    },
    {
      id: 'sidebar-search-target', display_title: '搜索目标会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T08:30:00Z', updated_at: '2026-09-12T08:30:00Z',
    },
  ];
  const search = {
    id: 'sidebar-search', query: '精准定位', state: 'SUCCEEDED',
    hits: [{
      binding_id: 'sidebar-search-target', event_id: 'sidebar-search-event', title: '搜索目标会话',
      source: 'agent', timestamp: '2026-09-12T08:30:00Z', content: '这是需要精准定位的搜索内容。',
    }],
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversation-activity')) return json(route, {
      running_binding_ids: ['sidebar-directory-running'],
      possibly_stuck_binding_ids: ['sidebar-directory-running'],
      failed_binding_ids: ['sidebar-root-unread'],
    });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/conversation-searches') && request.method() === 'POST') return json(route, search);
    if (path.endsWith('/conversation-searches/sidebar-search')) return json(route, search);
    if (path.endsWith('/events')) {
      const bindingId = path.split('/').at(-2)!;
      const eventId = bindingId === 'sidebar-search-target' ? 'sidebar-search-event' : `${bindingId}-event`;
      return json(route, {
        events: [{ id: eventId, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: bindingId === 'sidebar-search-target' ? '这是需要精准定位的搜索内容。' : `会话 ${bindingId}`, timestamp: now } }],
        next_cursor: null, history_cursor: null, result: { status: bindingId === 'sidebar-directory-running' ? 'RUNNING' : 'COMPLETED' },
      });
    }
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [directory],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/sidebar-root-unread');

  const pinnedConversation = page.getByRole('button', { name: '归属工作区会话', exact: true });
  await pinnedConversation.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '置顶' }).click();
  const pinnedSection = page.getByRole('region', { name: '置顶会话' });
  await expect(pinnedSection.getByRole('button', { name: '归属工作区会话', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('flowweave:agent-workspace-pinned:agent-workspace:sidebar-workspace'))).toContain('sidebar-directory-pinned');
  await expect(page.locator('.agent-workspace-group').filter({ hasText: '归属工作区' }).getByRole('button', { name: '归属工作区会话', exact: true })).toHaveCount(0);

  await page.reload();
  const persistedPinnedConversation = page.getByRole('region', { name: '置顶会话' }).getByRole('button', { name: '归属工作区会话', exact: true });
  await expect(persistedPinnedConversation).toBeVisible();
  await persistedPinnedConversation.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '取消置顶' }).click();
  await expect(page.getByRole('region', { name: '置顶会话' })).toHaveCount(0);
  await expect(page.locator('.agent-workspace-group').filter({ hasText: '归属工作区' }).getByRole('button', { name: '归属工作区会话', exact: true })).toBeVisible();

  const unreadConversation = page.getByRole('button', { name: '未读根会话', exact: true });
  await unreadConversation.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为未读' }).click();
  const stalledConversation = page.getByRole('button', { name: '运行中目标会话', exact: true });
  await stalledConversation.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为未读' }).click();
  await page.getByRole('button', { name: /查看活动会话（2）/ }).click();
  const activity = page.getByRole('region', { name: '活动会话' });
  await expect(activity).toBeVisible();
  await expect.poll(() => activity.locator('[data-conversation-binding-id]').evaluateAll(rows => rows.map(row => row.getAttribute('data-conversation-binding-id')))).toEqual([
    'sidebar-directory-running',
    'sidebar-root-unread',
  ]);
  await expect(activity.locator('[data-conversation-binding-id="sidebar-directory-running"]')).toContainText('归属工作区');
  await expect(activity.locator('[data-conversation-binding-id="sidebar-root-unread"]')).toContainText('根工作区');
  await expect(activity.locator('[data-conversation-binding-id="sidebar-directory-running"]').getByRole('img', { name: '后台长时间未产生可确认进展' })).toBeVisible();
  await expect(activity.locator('[data-conversation-binding-id="sidebar-directory-running"]').getByRole('img', { name: '会话有未读回复' })).toBeVisible();
  await expect(activity.locator('[data-conversation-binding-id="sidebar-root-unread"]').getByRole('img', { name: '会话异常结束' })).toBeVisible();
  await expect(activity.locator('[data-conversation-binding-id="sidebar-root-unread"]').getByRole('img', { name: '会话已完成，有未读回复' })).toHaveCount(0);

  const runningConversation = activity.getByRole('button', { name: '运行中目标会话', exact: true });
  await runningConversation.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-root-unread$/);
  await expect(activity).toBeVisible();
  await expect(runningConversation).toHaveClass(/active/);
  await expect(page.getByText('会话 sidebar-directory-running', { exact: true })).toBeVisible();

  await runningConversation.dblclick();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-directory-running$/);
  await expect(activity).toHaveCount(0);
  await expect(page.locator('[data-conversation-binding-id="sidebar-directory-running"]')).toHaveClass(/sidebar-reveal/);

  await page.getByRole('button', { name: '搜索会话' }).click();
  await page.getByLabel('搜索会话内容').fill('精准定位');
  await page.getByLabel('搜索会话内容').press('Enter');
  await page.getByRole('dialog').getByRole('button', { name: /搜索目标会话/ }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-search-target$/);
  await expect(page.locator('[data-conversation-event-id="sidebar-search-event"]')).toHaveClass(/conversation-search-target/);
});
