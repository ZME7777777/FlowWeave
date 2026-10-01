import { expect, test, type Page, type Route, type WebSocketRoute } from '@playwright/test';

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

test('Agent session keeps its conversation URL while refreshing a deep link', async ({ page }) => {
  let authenticated = false;
  let releaseConversation: (() => void) | undefined;
  const conversationRead = new Promise<void>(resolve => { releaseConversation = resolve; });
  const workspace = { id: 'deep-link-workspace', display_name: '深链工作区', desired_state: 'RUNNING', updated_at: now };
  const listedConversation = {
    id: 'another-conversation', display_title: '列表中的其他会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const restoredConversation = {
    id: 'refresh-target-conversation', display_title: '刷新后恢复的会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [listedConversation], next_cursor: null });
    if (path.endsWith('/conversations/refresh-target-conversation') && request.method() === 'GET') {
      await conversationRead;
      return json(route, restoredConversation);
    }
    if (path.endsWith('/hydration')) return json(route, {
      events: {
        events: [{ id: 'restored-agent-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: '已恢复刷新前的会话。', timestamp: now } }],
        next_cursor: 'restored-agent-reply', history_cursor: null, result: { status: 'COMPLETED' },
      },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 256, usage_current: true },
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
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/refresh-target-conversation');
  await expect(page.getByRole('button', { name: '列表中的其他会话', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/conversations\/refresh-target-conversation$/);
  releaseConversation?.();
  await expect(page.getByText('已恢复刷新前的会话。')).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/conversations\/refresh-target-conversation$/);
});


test('Accepted message reconciles its formal event without a second submission or reload', async ({ page }) => {
  let authenticated = false;
  let messageAccepted = false;
  let formalMessageVisible = false;
  let eventReadsAfterAcceptance = 0;
  let readinessReads = 0;
  let releaseMessageAcceptance: (() => void) | undefined;
  const messageAcceptance = new Promise<void>(resolve => { releaseMessageAcceptance = resolve; });
  const workspace = { id: 'stale-monitoring-workspace', display_name: '陈旧监控工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'stale-monitoring-conversation', display_title: '陈旧监控会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const staleMonitoring = {
    last_event_id: 'prior-turn', last_event_type: 'MESSAGE', last_event_at: '2026-09-12T09:28:00Z',
    seconds_since_event: 90, stale_after_seconds: 60, possibly_stuck: false, subagent_count: 0, active_subagents: [],
  };
  const events = () => [
    { id: 'prior-turn', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '上一轮请求', timestamp: '2026-09-12T09:28:00Z' } },
    { id: 'prior-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'prior-turn', content: '上一轮已完成', timestamp: '2026-09-12T09:29:00Z' } },
    ...(formalMessageVisible ? [
      { id: 'accepted-message', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'prior-reply', content: '刚发送的消息', timestamp: now } },
      { id: 'accepted-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'accepted-message', content: '正在执行当前任务', timestamp: now } },
    ] : []),
  ];

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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, {
      items: [{ ...conversation, execution_status: messageAccepted ? 'running' : 'idle' }], next_cursor: null,
    });
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: events(), next_cursor: null, history_cursor: null, monitoring: staleMonitoring },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
      readiness: { ready: !messageAccepted, execution_status: messageAccepted ? 'running' : 'idle' },
    });
    if (path.endsWith('/events')) {
      if (messageAccepted) {
        eventReadsAfterAcceptance += 1;
        if (eventReadsAfterAcceptance >= 2) formalMessageVisible = true;
      }
      return json(route, {
        events: events(), next_cursor: formalMessageVisible ? 'accepted-message' : 'prior-turn', history_cursor: null, monitoring: staleMonitoring,
      });
    }
    if (path.endsWith('/input-readiness')) {
      readinessReads += 1;
      // Model the stale terminal snapshot that can remain readable while the
      // accepted user event has not yet reached the formal event window.
      return json(route, { ready: true, execution_status: 'idle' });
    }
    if (path.endsWith('/messages') && request.method() === 'POST') {
      await messageAcceptance;
      messageAccepted = true;
      return json(route, { accepted: true, cursor: 'accepted-message' }, 202);
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
    if (path.endsWith('/model-providers')) return json(route, [{
      id: 'test-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }],
    }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/stale-monitoring-conversation');
  await page.getByLabel('发送 Agent 消息').fill('刚发送的消息');
  await page.getByRole('button', { name: '发送消息' }).click();
  const localMessage = page.locator('[data-user-event-id^="pending-user:"]');
  await expect(localMessage).toHaveCount(1);
  await expect(localMessage).toContainText('刚发送的消息');
  await expect(page.locator('[data-user-event-id="accepted-message"]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();

  releaseMessageAcceptance?.();
  await expect.poll(() => messageAccepted).toBe(true);
  await expect.poll(() => eventReadsAfterAcceptance).toBeGreaterThanOrEqual(2);
  // The preceding formal turn is complete and readiness is still stale idle,
  // but this accepted submission has not appeared in the event window yet.
  // It must retain the current turn's interrupt control until formal identity
  // catches up; otherwise the UI falsely offers a second send.
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toHaveCount(0);
  expect(readinessReads).toBeGreaterThan(0);
  await expect.poll(() => formalMessageVisible).toBe(true);
  await expect(page.getByText('正在执行当前任务', { exact: true })).toBeVisible();
  // Formal identity is now present, so the submission bridge has ended. The
  // stale terminal readiness must still not collapse this active formal branch.
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toHaveCount(0);
  await expect(localMessage).toHaveCount(1);
});


test('Generated conversation title updates both the sidebar and current header', async ({ page }) => {
  let authenticated = false;
  let generated = false;
  const workspace = {
    id: 'generated-title-workspace', display_name: '自动标题工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const initialConversation = {
    id: 'generated-title-conversation', display_title: '原始标题', title_state: 'PENDING',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };
  const generatedConversation = {
    ...initialConversation, display_title: '自动生成的标题', title_state: 'GENERATED' as const,
  };
  const alternateConversation = {
    ...initialConversation, id: 'generated-title-alternate-conversation', display_title: '另一条会话', title_state: 'MANUAL' as const,
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
      return json(route, { items: [generated ? generatedConversation : initialConversation, alternateConversation], next_cursor: null });
    }
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
    if (path.endsWith('/conversations/generated-title-conversation') && request.method() === 'GET') return json(route, generated ? generatedConversation : initialConversation);
    if (path.endsWith('/conversations/generated-title-alternate-conversation') && request.method() === 'GET') return json(route, alternateConversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/generated-title-conversation');
  const sidebarConversation = page.locator('[data-conversation-binding-id="generated-title-conversation"]');
  const headerTitle = page.locator('.agent-session-title');
  await expect(sidebarConversation).toContainText('原始标题');
  await expect(headerTitle).toHaveText('原始标题');

  generated = true;
  await expect(sidebarConversation).toContainText('自动生成的标题', { timeout: 5_000 });
  await expect(headerTitle).toHaveText('自动生成的标题');

  await page.locator('[data-conversation-binding-id="generated-title-alternate-conversation"]').dblclick();
  await expect(page.locator('.agent-session-title')).toHaveText('另一条会话');
  await page.locator('[data-conversation-binding-id="generated-title-conversation"]').dblclick();
  await expect(sidebarConversation).toContainText('自动生成的标题');
  await expect(headerTitle).toHaveText('自动生成的标题');
});


test('Completed conversation shows an explicit loading state without appearing to think', async ({ page }) => {
  let authenticated = false;
  const unreadWrites: Array<{ unread: boolean; unread_origin?: string }> = [];
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
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const body = request.postDataJSON() as { unread: boolean; unread_origin?: string };
      unreadWrites.push(body);
      return json(route, { ...conversation, unread: body.unread, unread_origin: body.unread ? body.unread_origin ?? 'MANUAL' : null });
    }
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
  await expect(page.locator('.conversation-live-task-plan')).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([]);

  releaseHydration?.();
  await expect(page.getByText('历史回复', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
  await expect(page.locator('.conversation-turn-status')).toHaveCount(0);
  await expect(page.locator('.conversation-live-task-plan')).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([]);
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
  let readinessReads = 0;
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
    if (path.endsWith('/input-readiness')) { readinessReads += 1; return json(route, { ready: false, execution_status: 'running' }); }
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
  await page.waitForTimeout(500);
  expect(readinessReads).toBeLessThanOrEqual(1);
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
  // Changing selection must not cancel-and-replace a still occupied server read.
  await page.waitForTimeout(500);
  expect(hydrationReads).toEqual(['rapid-switch-a']);
  await expect(page.getByText('回复 rapid-switch-a', { exact: true })).toHaveCount(0);
  releaseFirstHydration?.();
  await expect(page.getByText('回复 rapid-switch-c', { exact: true })).toBeVisible();
  expect(hydrationReads).toEqual(['rapid-switch-a', 'rapid-switch-c']);
  await expect(page.getByText('回复 rapid-switch-a', { exact: true })).toHaveCount(0);
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


test('Foreground recovery reconciles a stale terminal snapshot for a running conversation', async ({ page }) => {
  let authenticated = false;
  let runtimeRunning = false;
  let eventReads = 0;
  const workspace = {
    id: 'foreground-recovery-workspace', display_name: '前台恢复工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'foreground-recovery-conversation', display_title: '后台仍在运行的会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const terminalEvents = {
    events: [
      { id: 'foreground-old-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '上一轮已完成请求', timestamp: now } },
      { id: 'foreground-old-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'foreground-old-user', content: '上一轮回复', timestamp: now } },
    ],
    next_cursor: 'foreground-old-agent', history_cursor: null, result: { status: 'COMPLETED' },
  };
  const runningEvents = {
    events: [
      ...terminalEvents.events,
      { id: 'foreground-running-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'foreground-old-agent', content: '后台继续处理的请求', timestamp: now } },
    ],
    next_cursor: 'foreground-running-user', history_cursor: null, result: { status: 'RUNNING' },
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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, {
      items: [{ ...conversation, execution_status: runtimeRunning ? 'running' : 'idle' }], next_cursor: null,
    });
    if (path.endsWith('/conversation-activity')) return json(route, {
      running_binding_ids: runtimeRunning ? [conversation.id] : [],
    });
    if (path.endsWith('/hydration')) return json(route, {
      events: terminalEvents,
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/events')) {
      eventReads += 1;
      return json(route, runtimeRunning ? runningEvents : terminalEvents);
    }
    if (path.endsWith('/input-readiness')) return json(route, {
      ready: !runtimeRunning, execution_status: runtimeRunning ? 'running' : 'idle',
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
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/foreground-recovery-conversation');
  await expect(page.getByText('上一轮回复', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
  const readsBeforeForeground = eventReads;

  runtimeRunning = true;
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });

  await expect.poll(() => eventReads).toBeGreaterThan(readsBeforeForeground);
  await expect(page.getByText('后台继续处理的请求', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(page.locator('[data-conversation-binding-id="foreground-recovery-conversation"] .agent-workspace-conversation-running')).toBeVisible();
});


test('Native terminal readiness overrides a stale activity running projection', async ({ page }) => {
  let authenticated = false;
  const workspace = {
    id: 'terminal-readiness-workspace', display_name: '终态状态工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'terminal-readiness-conversation', display_title: '已完成目标会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const terminalEvents = {
    events: [
      { id: 'terminal-readiness-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '已完成的请求', timestamp: now } },
      { id: 'terminal-readiness-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'terminal-readiness-user', content: '已完成的回复', timestamp: now } },
    ],
    next_cursor: 'terminal-readiness-agent', history_cursor: null, result: { status: 'COMPLETED' },
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
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [conversation.id] });
    if (path.endsWith('/hydration')) return json(route, {
      events: terminalEvents,
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) return json(route, terminalEvents);
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
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
  await page.goto('/agent/conversations/terminal-readiness-conversation');

  await expect(page.getByText('已完成的回复', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  const row = page.locator('[data-conversation-binding-id="terminal-readiness-conversation"]');
  await expect(row.locator('.agent-workspace-conversation-running')).toHaveCount(0);
  await expect(row.locator('.agent-workspace-conversation-unread')).toHaveCount(0);
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
  const deletingRow = page.locator('[data-conversation-binding-id="optimistic-delete-a"]');
  await deletingRow.hover();
  await expect(deletingRow.getByRole('button', { name: '删除会话 待删除会话' })).toBeVisible();
  await expect(deletingRow.locator('.agent-workspace-conversation-drag')).toBeVisible();
  await expect(deletingRow.locator('.agent-workspace-conversation-select svg')).toBeHidden();
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


test('Sidebar chat tabs stay isolated to their source conversation', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'sidebar-chat-scope-workspace', display_name: '侧边聊天隔离工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['sidebar-chat-scope-a', 'sidebar-chat-scope-b'].map((id, index) => ({
    id, display_title: `侧边聊天会话 ${String.fromCharCode(65 + index)}`, title_state: 'MANUAL',
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
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/model-providers')) return json(route, [{
      id: 'test-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }],
    }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/sidebar-chat-scope-a');
  await page.locator('.agent-workspace-summary').getByRole('button', { name: '侧边聊天', exact: true }).click();
  await expect(page.getByRole('region', { name: '侧边聊天' })).toBeVisible();
  await expect(page.getByText('向主会话追问', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '打开侧边聊天', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '删除会话', exact: true })).toHaveCount(0);
  await page.getByLabel('关闭工作区工具').click();
  await expect(page.getByRole('region', { name: '侧边聊天' })).toHaveCount(0);
  await expect(page.getByText('环境信息', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '侧边聊天会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-chat-scope-b$/);
  await expect(page.getByRole('region', { name: '侧边聊天' })).toHaveCount(0);

  await page.getByRole('button', { name: '侧边聊天会话 A', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-chat-scope-a$/);
  await expect(page.getByRole('region', { name: '侧边聊天' })).toHaveCount(0);
});


test('Sidebar chat accepts selected and pasted attachments before its first message', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'sidebar-attachments-workspace', display_name: '侧边附件工作区', desired_state: 'RUNNING', updated_at: now };
  const source = {
    id: 'sidebar-attachments-source', display_title: '主会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true,
    execution_status: 'idle', created_at: now, updated_at: now,
  };
  const sidebar = { ...source, id: 'sidebar-attachments-draft', display_title: '侧边临时聊天' };
  const completedUploads: string[] = [];
  const uploadRequests: string[] = [];
  let firstMessage: Record<string, unknown> | undefined;

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [source], next_cursor: null });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'test-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true, supported_reasoning_efforts: [] }] }]);
    if (path.endsWith('/hydration')) return json(route, { events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 0, usage_current: true }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/attachments/uploads')) uploadRequests.push(`${request.method()} ${path}`);
    if (path.includes('/attachments/uploads') && request.method() === 'POST' && !path.endsWith('/complete')) return json(route, { upload_id: `sidebar-upload-${completedUploads.length}`, chunk_size: 262_144, uploaded_parts: [] }, 201);
    if (path.includes('/attachments/uploads') && request.method() === 'PUT') return route.fulfill({ status: 200 });
    if (path.includes('/attachments/uploads') && path.endsWith('/complete') && request.method() === 'POST') {
      const filename = completedUploads.length === 0 ? 'selected.txt' : 'pasted.png';
      completedUploads.push(filename);
      return json(route, { filename, mime_type: filename.endsWith('.png') ? 'image/png' : 'text/plain', byte_size: 4, path: `/runtime/workspace/project/uploads/sidebar-attachments-draft-${filename}` }, 201);
    }
    if (path.endsWith('/sidebar') && request.method() === 'POST') {
      firstMessage = request.postDataJSON() as Record<string, unknown>;
      return json(route, { conversation: sidebar, accepted: true, expires_at: new Date(Date.now() + 3_600_000).toISOString() }, 201);
    }
    if (path.includes('/sidebars/')) return json(route, { binding_id: sidebar.id, source_binding_id: source.id, expires_at: new Date(Date.now() + 3_600_000).toISOString(), expired: false });
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, path.endsWith(sidebar.id) ? sidebar : source);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${source.id}`);
  await page.locator('.agent-workspace-summary').getByRole('button', { name: '侧边聊天', exact: true }).click();
  const sidebarPane = page.getByRole('region', { name: '侧边聊天' });
  await expect(sidebarPane.getByLabel('添加附件')).toBeEnabled();
  await sidebarPane.getByLabel('上传侧边聊天附件').setInputFiles({ name: 'selected.txt', mimeType: 'text/plain', buffer: Buffer.from('file') });
  await expect.poll(() => uploadRequests).toHaveLength(3);
  await expect(sidebarPane.getByText('selected.txt', { exact: true })).toBeVisible();
  const sidebarInput = sidebarPane.getByRole('textbox', { name: '发送侧边聊天消息' });
  await expect(sidebarInput.locator('xpath=..')).toHaveClass(/agent-composer-input/);
  await sidebarInput.evaluate(element => {
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File(['png'], 'pasted.png', { type: 'image/png' }));
    element.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData }));
  });
  await expect.poll(() => uploadRequests).toHaveLength(6);
  await expect(sidebarPane.getByText('pasted.png', { exact: true })).toBeVisible();
  await sidebarInput.fill('请分析两个附件');
  await sidebarPane.getByRole('button', { name: '发送侧边聊天消息' }).click();
  await expect(sidebarPane.locator('.conversation-message.user')).toContainText('请分析两个附件');
  await expect.poll(() => firstMessage).toMatchObject({
    content: '请分析两个附件',
    attachments: [{ filename: 'selected.txt' }, { filename: 'pasted.png' }],
  });
  expect(firstMessage).not.toHaveProperty('model_provider_id');
  expect(firstMessage).not.toHaveProperty('model_name');
  expect(firstMessage).not.toHaveProperty('reasoning_effort');
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

test('Workspace file references open in a preview dialog', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'workspace-reference-preview-workspace', display_name: '本地引用预览工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'workspace-reference-preview-conversation', display_title: '本地引用预览会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };
  const reference = {
    path: '/runtime/workspace/project/designs/batch-plan.md', kind: 'file', display_name: 'batch-plan.md',
  };
  const previewPaths: string[] = [];

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
      events: [{ id: 'workspace-reference-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请查看本地方案', workspace_references: [reference], timestamp: now } }],
      next_cursor: 'workspace-reference-user', history_cursor: null, result: { status: 'COMPLETED' },
    });
    if (path.endsWith('/file') && url.searchParams.get('preview') === 'true') {
      previewPaths.push(url.searchParams.get('path') ?? '');
      return route.fulfill({ status: 200, contentType: 'text/markdown', headers: { 'X-Preview-Total-Bytes': '21' }, body: '# 批次方案\n\n预览成功。' });
    }
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [{ kind: 'file', path: reference.path, name: reference.display_name, size: 21 }], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
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
  await page.goto('/agent/conversations/workspace-reference-preview-conversation');
  await page.getByRole('button', { name: 'batch-plan.md', exact: true }).click();

  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('batch-plan.md');
  await expect(preview).toContainText('预览成功。');
  expect(previewPaths).toEqual([reference.path]);
});




test('A delayed message response never renders in another conversation', async ({ page }) => {
  let authenticated = false;
  let releaseSend: (() => void) | undefined;
  let sentBindingId: string | undefined;
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
      if (bindingId === 'send-switch-a') await sendGate;
      return json(route, { accepted: true, cursor: `${bindingId}-user-sent` });
    }
    if (path.endsWith('/events')) {
      const id = path.split('/').at(-2)!;
      const events = id === 'send-switch-b'
        ? [{ id: `${id}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '会话 B 正在处理已有请求', timestamp: now } }]
        : [{ id: `${id}-initial`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: `初始消息 ${id}`, timestamp: now } }];
      return json(route, {
        events, next_cursor: events.at(-1)!.id, history_cursor: null, result: { status: 'COMPLETED' },
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
    if (path.endsWith('/input-readiness')) {
      const bindingId = path.split('/').at(-2)!;
      return json(route, bindingId === 'send-switch-b'
        ? { ready: false, execution_status: 'running' }
        : { ready: true, execution_status: 'idle' });
    }
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
  await expect(page.getByText('会话 B 正在处理已有请求')).toBeVisible();
  await expect(page.getByText('正在思考', { exact: true })).toBeVisible();
  await expect(page.getByText('正在提交消息', { exact: true })).toHaveCount(0);
  await expect(page.getByText('只属于会话 A 的消息')).toHaveCount(0);
  await expect(composer).toBeEditable();

  releaseSend?.();
  await expect.poll(() => page.getByText('只属于会话 A 的消息').count()).toBe(0);
  await expect(page.getByText('会话 B 正在处理已有请求')).toBeVisible();
  await expect(page.getByText('正在提交消息', { exact: true })).toHaveCount(0);
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

  await surface.evaluate(element => { element.dataset.refreshScrollWrites = '0'; });
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-tool-one', 'TOOL_CALL', {
      source: 'agent', parent_id: 'scroll-a-user', action_id: 'scroll-tool-one', tool_call_id: 'scroll-call-one',
      tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'git status --short' },
    }),
  }));
  await expect(page.getByText('正在运行 git status --short')).toBeVisible();
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(surface).toHaveAttribute('data-refresh-scroll-writes', '1');
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
  let uploadedDraftId: string | null = null;
  let discardedDraftId: string | null = null;
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
    if (path.includes('/attachments/uploads') && request.method() === 'POST' && !path.endsWith('/complete')) {
      uploadedDraftId = (JSON.parse(request.postData() ?? '{}') as { conversation_id?: string }).conversation_id ?? null;
      return json(route, { upload_id: 'draft-race-upload', chunk_size: 262_144, uploaded_parts: [] }, 201);
    }
    if (path.includes('/attachments/uploads/draft-race-upload') && request.method() === 'PUT') return route.fulfill({ status: 200 });
    if (path.includes('/attachments/uploads/draft-race-upload/complete') && request.method() === 'POST') return json(route, {
      filename: '新会话附件.txt', mime_type: 'text/plain', byte_size: 9,
      path: `/runtime/workspace/project/uploads/${uploadedDraftId}-attachment--新会话附件.txt`,
    }, 201);
    if (path.includes('/draft-attachments/') && request.method() === 'DELETE') {
      discardedDraftId = decodeURIComponent(path.split('/').at(-1)!);
      return route.fulfill({ status: 204, body: '' });
    }
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
  // An unsubmitted draft owns no native Conversation. Even if the previous
  // selected binding is still unwinding, it must remain editable and idle.
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  await expect(page.getByText('正在同步 Agent 状态', { exact: true })).toHaveCount(0);
  await expect(page.getByText('正在提交消息', { exact: true })).toHaveCount(0);
  const composer = page.getByLabel('发送 Agent 消息');
  const draftAttachment = page.locator('.agent-composer .agent-attachments').getByText('新会话附件.txt', { exact: true });
  await page.getByLabel('上传附件').setInputFiles({ name: '新会话附件.txt', mimeType: 'text/plain', buffer: Buffer.from('new-draft') });
  await expect(draftAttachment).toBeVisible();
  await expect.poll(() => page.evaluate(() => JSON.stringify(Object.entries(localStorage)))).toContain('新会话附件.txt');
  const recoverDraft = page.getByRole('button', { name: '恢复根工作区的未发送草稿' });
  await page.reload();
  await expect(recoverDraft).toBeVisible();
  await recoverDraft.click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(composer).toHaveText('');
  await expect(draftAttachment).toBeVisible();
  await composer.fill('只属于新会话的未发送草稿');

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await page.getByRole('button', { name: '竞态会话 C', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/draft-race-c$/);
  await expect(composer).toHaveText('');
  await expect(draftAttachment).toHaveCount(0);

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await expect(composer).toHaveText('');
  await expect(draftAttachment).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => Object.entries(localStorage)
    .filter(([key]) => key.includes('draft-race-workspace:draft-race-'))
    .every(([, value]) => !value.includes('只属于新会话的未发送草稿') && !value.includes('新会话附件.txt')))).toBe(true);

  await recoverDraft.click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(composer).toHaveText('只属于新会话的未发送草稿');
  await expect(draftAttachment).toBeVisible();

  await page.getByRole('button', { name: '竞态会话 B', exact: true }).click();
  await page.reload();
  await recoverDraft.click();
  await expect(composer).toHaveText('只属于新会话的未发送草稿');
  await expect(draftAttachment).toBeVisible();

  await page.getByRole('button', { name: '在根工作区中新建会话' }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(composer).toHaveText('');
  await expect(draftAttachment).toHaveCount(0);
  await expect(recoverDraft).toHaveCount(0);
  await expect.poll(() => discardedDraftId).toBe(uploadedDraftId);
});

test('First message removes the matching recoverable draft from the conversation rail', async ({ page }) => {
  let authenticated = false;
  let createdConversation: Record<string, unknown> | undefined;
  const workspace = { id: 'draft-bootstrap-workspace', display_name: '草稿发送工作区', desired_state: 'RUNNING', updated_at: now };
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: createdConversation ? [createdConversation] : [], next_cursor: null });
    if (path.endsWith('/conversations') && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() ?? '{}') as { conversation_id: string };
      createdConversation = {
        id: payload.conversation_id, display_title: '发送草稿', title_state: 'PENDING', lifecycle: 'ACTIVE',
        model_provider_id: 'draft-bootstrap-provider', model_name: 'draft-bootstrap-model', reasoning_effort: null,
        streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
      };
      return json(route, { conversation: createdConversation, accepted: true, cursor: 'draft-bootstrap-event' }, 201);
    }
    if (/\/conversations\/[^/]+$/.test(path) && request.method() === 'GET') return json(route, createdConversation);
    if (path.endsWith('/events')) return json(route, {
      events: [{ id: 'draft-bootstrap-event', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '发送这个草稿', timestamp: now } }],
      next_cursor: 'draft-bootstrap-event', history_cursor: null, result: { status: 'RUNNING' },
    });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'draft-bootstrap-model', window_tokens: 128_000, used_tokens: 0, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'draft-bootstrap-provider', name: '草稿模型', connection_state: 'CONNECTED', models: [{ model_name: 'draft-bootstrap-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  const draftId = '10000000-0000-4000-8000-000000000099';
  await page.evaluate(({ key, value }) => localStorage.setItem(key, JSON.stringify(value)), {
    key: `flowweave:agent-conversation-draft:v2:agent-workspace:${workspace.id}:root`,
    value: {
      draft: { id: draftId, displayName: '根工作区', capabilityVersionIds: [] },
      content: '发送这个草稿', attachments: [], references: [], workspaceReferences: [], annotations: [],
      providerId: 'draft-bootstrap-provider', modelName: 'draft-bootstrap-model', reasoningEffort: null,
    },
  });
  await page.goto('/agent');
  const recoverDraft = page.getByRole('button', { name: '恢复根工作区的未发送草稿' });
  await expect(recoverDraft).toBeVisible();
  await recoverDraft.click();
  await page.getByLabel('发送消息').click();

  await expect.poll(() => createdConversation?.id).toBeTruthy();
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
  const runningIndicator = backgroundRow.locator('.agent-workspace-conversation-running');
  await expect(runningIndicator).toBeVisible();
  await expect(selectedRow.locator('.agent-workspace-conversation-running')).toHaveCount(0);

  await backgroundRow.getByRole('button', { name: '后台运行会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/background-running-conversation$/);
  await expect(backgroundRow).toHaveClass(/active/);
  await backgroundRow.hover();
  await expect(runningIndicator).toBeVisible();
});


test('A dropped running-session stream immediately reconciles formal events', async ({ page }) => {
  let authenticated = false;
  let eventsAfterDisconnect = 0;
  let stream: WebSocketRoute | undefined;
  const workspace = {
    id: 'stream-recovery-workspace', display_name: '断流恢复工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'stream-recovery-conversation', display_title: '断流中的运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const runningEvents = {
    events: [{ id: 'stream-recovery-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '持续处理这个任务', timestamp: now } }],
    next_cursor: 'stream-recovery-user', history_cursor: null, result: { status: 'RUNNING' },
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
      events: runningEvents,
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) {
      eventsAfterDisconnect += 1;
      return json(route, runningEvents);
    }
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [conversation.id] });
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
  await page.goto('/agent/conversations/stream-recovery-conversation');
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect.poll(() => stream).toBeTruthy();

  await stream!.close({ code: 1011, reason: 'transient stream failure' });
  await expect(page.getByText('连接恢复中', { exact: true })).toBeVisible();
  await expect.poll(() => eventsAfterDisconnect).toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
});


test('A formal final reply releases the shared composer state for queued delivery', async ({ page }) => {
  let authenticated = false;
  let stream: WebSocketRoute | undefined;
  let queuedPosts = 0;
  const workspace = { id: 'final-reply-queue-workspace', display_name: '最终回复队列工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'final-reply-queue-conversation', display_title: '最终回复队列会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const runningEvents = {
    events: [
      { id: 'final-reply-queue-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请完成当前回复', timestamp: now } },
      { id: 'final-reply-queue-action', event_type: 'TOOL_CALL', payload: { parent_id: 'final-reply-queue-user', action_id: 'final-reply-queue-action', tool_call_id: 'final-reply-queue-call', tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'pwd' }, timestamp: now } },
    ],
    next_cursor: 'final-reply-queue-action', history_cursor: null, result: { status: 'RUNNING' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', route => { stream = route; });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [conversation.id] });
    if (path.endsWith('/hydration')) return json(route, {
      events: runningEvents,
      context: { model_name: 'queue-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) return json(route, runningEvents);
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/messages') && request.method() === 'POST') {
      queuedPosts += 1;
      return json(route, { accepted: true, cursor: 'final-reply-queued-user' }, 202);
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/context')) return json(route, { model_name: 'queue-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'queue-provider', name: '队列模型', connection_state: 'CONNECTED', models: [{ model_name: 'queue-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/final-reply-queue-conversation');
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect.poll(() => stream).toBeTruthy();

  await composer.fill('最终回复后自动发送');
  await composer.press('Enter');
  await expect(page.getByLabel('消息投递队列').getByText('最终回复后自动发送')).toBeVisible();
  expect(queuedPosts).toBe(0);

  stream!.send(JSON.stringify({
    type: 'event',
    event: { id: 'final-reply-queue-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'final-reply-queue-action', content: '当前轮已经完成。', timestamp: now } },
  }));

  await expect(page.getByText('当前轮已经完成。')).toBeVisible();
  await expect.poll(() => queuedPosts).toBe(1);
  await expect(page.getByLabel('消息投递队列')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
});


test('Stream message completion keeps a native running conversation active', async ({ page }) => {
  let authenticated = false;
  let stream: WebSocketRoute | undefined;
  const workspace = { id: 'message-complete-workspace', display_name: '完成事件工作区', desired_state: 'RUNNING', updated_at: now };
  const activeConversation = {
    id: 'message-complete-active', display_title: '当前完成会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const backgroundConversation = {
    id: 'message-complete-background', display_title: '后台运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const runningEvents = {
    events: [
      { id: 'message-complete-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请完成当前回复', timestamp: now } },
      { id: 'message-complete-plan', event_type: 'TOOL_CALL', payload: { parent_id: 'message-complete-user', action_id: 'message-complete-plan', tool_call_id: 'message-complete-plan-call', tool_name: 'task_tracker', event_name: 'TaskTrackerAction', details: { command: 'plan', task_list: [{ title: '保持底部计划布局', notes: '等待正式终态事件。', status: 'in_progress' }] }, timestamp: now } },
      { id: 'message-complete-plan-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'message-complete-plan', action_id: 'message-complete-plan', tool_call_id: 'message-complete-plan-call', tool_name: 'task_tracker', event_name: 'TaskTrackerObservation', details: { command: 'plan', is_error: false, task_list: [{ title: '保持底部计划布局', notes: '等待正式终态事件。', status: 'in_progress' }] }, timestamp: now } },
    ],
    next_cursor: 'message-complete-plan-result', history_cursor: null, result: { status: 'RUNNING' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', route => { stream = route; });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [activeConversation, backgroundConversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [activeConversation.id, backgroundConversation.id] });
    if (path.endsWith('/hydration')) return json(route, {
      events: runningEvents,
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) return json(route, runningEvents);
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, activeConversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/message-complete-active');
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(page.getByLabel('任务：0 / 1 已完成')).toBeVisible();
  await expect.poll(() => stream).toBeTruthy();
  await expect(page.locator('[data-conversation-binding-id="message-complete-background"] .agent-workspace-conversation-running')).toBeVisible();

  stream!.send(JSON.stringify({ type: 'message_complete' }));

  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect(page.getByLabel('任务：0 / 1 已完成')).toBeVisible();
  await expect(page.locator('[data-conversation-binding-id="message-complete-active"] .agent-workspace-conversation-running')).toBeVisible();
  await expect(page.locator('[data-conversation-binding-id="message-complete-background"] .agent-workspace-conversation-running')).toBeVisible();
});




test('Terminal Agent error renders its detail and restores older history pages', async ({ page }) => {
  let authenticated = false;
  let historyRequests = 0;
  const workspace = {
    id: 'running-history-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'running-history-conversation', display_title: '终态异常历史会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, execution_status: 'idle', created_at: now, updated_at: now,
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
          { id: 'terminal-agent-error', event_type: 'ERROR', payload: { source_type: 'AgentErrorEvent', parent_id: 'live-user', event_name: 'AgentErrorEvent', content: 'AgentErrorEvent: context transport failed after the last tool result.', classification: { kind: 'internal' }, timestamp: now } },
        ],
        next_cursor: 'terminal-agent-error', history_cursor: 'compressed-history-page', result: { status: 'COMPLETED' },
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
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/running-history-conversation');
  await expect(page.getByText('当前仍在处理的请求')).toBeVisible();
  await expect(page.getByText('AgentErrorEvent: context transport failed after the last tool result.')).toBeVisible();
  await page.reload();
  await expect(page.getByText('当前仍在处理的请求')).toBeVisible();
  await expect(page.getByText('AgentErrorEvent: context transport failed after the last tool result.')).toBeVisible();
  await expect.poll(() => historyRequests).toBeGreaterThan(0);
  await expect(page.getByText('压缩前仍可见的历史会话')).toBeVisible();
  const completedHistoryRequests = historyRequests;
  await page.waitForTimeout(4_750);
  expect(historyRequests).toBe(completedHistoryRequests);
});


test('A normal background conversation completion persists a regular unread marker', async ({ page }) => {
  let authenticated = false;
  let completed = false;
  const unreadWrites: Array<{ id: string; unread: boolean; unread_origin?: string }> = [];
  const workspace = { id: 'completed-unread-workspace', display_name: '完成未读工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = [
    {
      id: 'completed-unread-current', display_title: '当前会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle', unread: false, created_at: now, updated_at: now,
    },
    {
      id: 'completed-unread-background', display_title: '后台完成会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'unknown', unread: false, unread_origin: null, created_at: now, updated_at: now,
    },
  ];

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversation-activity')) {
      if (completed) {
        const conversation = conversations.find(item => item.id === 'completed-unread-background')!;
        conversation.unread = true;
        conversation.unread_origin = 'MANUAL';
      }
      return json(route, { running_binding_ids: completed ? [] : ['completed-unread-background'] });
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const id = path.split('/').at(-2)!;
      const conversation = conversations.find(item => item.id === id)!;
      const body = request.postDataJSON() as { unread: boolean; unread_origin?: string };
      conversation.unread = body.unread;
      conversation.unread_origin = body.unread ? body.unread_origin ?? 'MANUAL' : null;
      unreadWrites.push({ id, ...body });
      return json(route, conversation);
    }
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const id = path.split('/').at(-1)!;
      return json(route, conversations.find(item => item.id === id));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/completed-unread-current');
  const backgroundRow = page.locator('[data-conversation-binding-id="completed-unread-background"]');
  await expect(backgroundRow.locator('.agent-workspace-conversation-running')).toBeVisible();

  completed = true;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));

  await expect(backgroundRow.locator('.agent-workspace-conversation-running')).toHaveCount(0);
  await expect(backgroundRow.getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();
  await expect(backgroundRow.locator('.agent-workspace-conversation-alert')).toHaveCount(0);

  await page.reload();
  await expect(page.locator('[data-conversation-binding-id="completed-unread-background"]')
    .getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();
  expect(unreadWrites).toHaveLength(0);
});


test('Resuming an unread conversation clears its unread marker before it renders as running', async ({ page }) => {
  let authenticated = false;
  let resumed = false;
  const unreadWrites: Array<{ unread: boolean; unread_origin?: string }> = [];
  const workspace = { id: 'resume-unread-workspace', display_name: '继续未读工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'resume-unread-conversation', display_title: '待继续未读会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'paused', unread: true, unread_origin: 'SYSTEM', created_at: now, updated_at: now,
  };
  const events = {
    events: [
      { id: 'resume-unread-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '继续前的任务', timestamp: now } },
    ],
    next_cursor: 'resume-unread-user', history_cursor: null, result: { status: 'PAUSED' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [{ ...conversation, execution_status: resumed ? 'running' : 'paused' }], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: resumed ? [conversation.id] : [] });
    if (path.endsWith('/hydration')) return json(route, {
      events: { ...events, result: { status: resumed ? 'RUNNING' : 'PAUSED' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: !resumed, execution_status: resumed ? 'running' : 'paused' },
    });
    if (path.endsWith('/resume') && request.method() === 'POST') {
      resumed = true;
      return json(route, { accepted: true, cursor: 'resume-unread-user' });
    }
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const body = request.postDataJSON() as { unread: boolean; unread_origin?: string };
      conversation.unread = body.unread;
      conversation.unread_origin = body.unread ? body.unread_origin ?? 'MANUAL' : null;
      unreadWrites.push(body);
      return json(route, conversation);
    }
    if (path.endsWith('/events')) return json(route, { ...events, result: { status: resumed ? 'RUNNING' : 'PAUSED' } });
    if (path.endsWith('/input-readiness')) return json(route, { ready: !resumed, execution_status: resumed ? 'running' : 'paused' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/resume-unread-conversation');
  const row = page.locator('[data-conversation-binding-id="resume-unread-conversation"]');
  await expect(page.getByRole('button', { name: '继续当前 Agent' })).toBeVisible();
  await expect(row.getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();

  await page.getByRole('button', { name: '继续当前 Agent' }).click();

  await expect.poll(() => unreadWrites).toEqual([{ unread: false }]);
  await expect(row.locator('.agent-workspace-conversation-running')).toBeVisible();
  await expect(row.locator('.agent-workspace-conversation-unread')).toHaveCount(0);
});


test('Sending a new message clears an unread marker before the conversation renders as running', async ({ page }) => {
  let authenticated = false;
  let sent = false;
  const unreadWrites: Array<{ unread: boolean; unread_origin?: string }> = [];
  const workspace = { id: 'send-unread-workspace', display_name: '发送未读工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'send-unread-conversation', display_title: '异常后待继续会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', unread: true, unread_origin: 'SYSTEM', created_at: now, updated_at: now,
  };
  const events = {
    events: [{ id: 'prior-error', event_type: 'ERROR', payload: { error_code: 'BadGatewayError', content: '上一轮模型服务不可用', timestamp: now } }],
    next_cursor: 'prior-error', history_cursor: null, result: { status: 'ERROR' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [{ ...conversation, execution_status: sent ? 'running' : 'idle' }], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: sent ? [conversation.id] : [] });
    if (path.endsWith('/hydration')) return json(route, {
      events: { ...events, result: { status: sent ? 'RUNNING' : 'ERROR' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: !sent, execution_status: sent ? 'running' : 'idle' },
    });
    if (path.endsWith('/messages') && request.method() === 'POST') {
      sent = true;
      return json(route, { accepted: true, cursor: 'continued-message' }, 202);
    }
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const body = request.postDataJSON() as { unread: boolean; unread_origin?: string };
      conversation.unread = body.unread;
      conversation.unread_origin = body.unread ? body.unread_origin ?? 'MANUAL' : null;
      unreadWrites.push(body);
      return json(route, conversation);
    }
    if (path.endsWith('/events')) return json(route, { ...events, result: { status: sent ? 'RUNNING' : 'ERROR' } });
    if (path.endsWith('/input-readiness')) return json(route, { ready: !sent, execution_status: sent ? 'running' : 'idle' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/send-unread-conversation');
  const row = page.locator('[data-conversation-binding-id="send-unread-conversation"]');
  await expect(row.getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();

  await page.getByLabel('发送 Agent 消息').fill('请从失败处继续');
  await page.getByRole('button', { name: '发送消息' }).click();

  await expect.poll(() => unreadWrites).toEqual([{ unread: false }]);
  await expect(row.locator('.agent-workspace-conversation-running')).toBeVisible();
  await expect(row.locator('.agent-workspace-conversation-unread')).toHaveCount(0);
});

test('Opening a normal-list unread conversation marks it read while activity preview remains read-only', async ({ page }) => {
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

  await conversationA.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-a$/);
  await expect(unreadMarker).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
  ]);

  await conversationA.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为未读' }).click();
  await expect(unreadMarker).toBeVisible();

  await page.getByRole('button', { name: /查看活动会话/ }).click();
  const activity = page.getByRole('region', { name: '活动会话' });
  const activityConversationA = activity.getByRole('button', { name: '未读会话 A', exact: true });
  await activityConversationA.click();
  await expect(activity).toBeVisible();
  await page.mouse.move(1000, 200);
  await expect(activityConversationA.locator('xpath=..').getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
    { id: 'unread-conversation-a', unread: true },
  ]);

  await activityConversationA.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为已读' }).click();
  await expect(activityConversationA).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
  ]);

  await page.getByRole('button', { name: '返回工作区列表' }).click();
  await conversationA.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '标记为未读' }).click();
  await page.getByRole('button', { name: /查看活动会话/ }).click();
  await expect(activityConversationA).toBeVisible();

  await activityConversationA.dblclick();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-a$/);
  await expect(activity).toHaveCount(0);
  await expect(unreadMarker).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
    { id: 'unread-conversation-a', unread: true },
    { id: 'unread-conversation-a', unread: false },
  ]);
});

test('Opening an unread conversation keeps it read when an older list request finishes later', async ({ page }) => {
  let authenticated = false;
  let listReads = 0;
  let staleListDelivered = false;
  const unreadWrites: Array<{ id: string; unread: boolean }> = [];
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
      const { unread } = request.postDataJSON() as { unread: boolean };
      conversation.unread = unread;
      unreadWrites.push({ id, unread });
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
  await expect(page).toHaveURL(/\/agent\/conversations\/stale-unread-conversation-a$/);
  await expect(unreadMarker).toHaveCount(0);
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'stale-unread-conversation-a', unread: false },
  ]);
  await conversationB.click();
  await expect.poll(() => staleListDelivered).toBe(true);
  await expect(unreadMarker).toHaveCount(0);
});

test('Conversation sidebar persists pinned sessions, orders activity, and reveals the selected source row', async ({ page }) => {
  let authenticated = false;
  let runningConversationPossiblyStuck = true;
  const workspace = { id: 'sidebar-workspace', display_name: '侧栏工作区', desired_state: 'RUNNING', updated_at: now };
  const directory = {
    id: 'sidebar-directory', display_name: '归属工作区',
    current_version: { working_directory: '/runtime/workspace/project/directory' },
  };
  const conversations = [
    {
      id: 'sidebar-root-unread', display_title: '未读根会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      unread: true, unread_origin: 'SYSTEM',
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
      unread: true, unread_origin: 'SYSTEM',
      created_at: '2026-09-12T09:30:00Z', updated_at: '2026-09-12T09:50:00Z',
    },
    {
      id: 'sidebar-search-target', display_title: '搜索目标会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T08:30:00Z', updated_at: '2026-09-12T08:30:00Z',
    },
  ];
  const unreadWrites: Array<{ id: string; unread: boolean }> = [];
  const pinnedWrites: Array<{ id: string; pinned: boolean }> = [];
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
      possibly_stuck_binding_ids: runningConversationPossiblyStuck ? ['sidebar-directory-running'] : [],
      failed_binding_ids: ['sidebar-root-unread'],
    });
    if (path.endsWith('/pinned') && request.method() === 'PUT') {
      const bindingId = path.split('/').at(-2)!;
      const conversation = conversations.find(item => item.id === bindingId)!;
      conversation.pinned = Boolean(request.postDataJSON().pinned);
      pinnedWrites.push({ id: bindingId, pinned: conversation.pinned });
      return json(route, conversation);
    }
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const bindingId = path.split('/').at(-2)!;
      const conversation = conversations.find(item => item.id === bindingId)!;
      conversation.unread = Boolean(request.postDataJSON().unread);
      conversation.unread_origin = request.postDataJSON().unread_origin
        ?? (conversation.unread ? 'MANUAL' : null);
      unreadWrites.push({ id: bindingId, unread: conversation.unread });
      return json(route, conversation);
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      const workDirectoryId = new URL(request.url()).searchParams.get('work_directory_id');
      return json(route, {
        items: conversations.filter(item => (item.work_directory_id ?? null) === workDirectoryId),
        next_cursor: null,
      });
    }
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
  await expect.poll(() => pinnedWrites).toEqual([{ id: 'sidebar-directory-pinned', pinned: true }]);
  await expect(page.locator('.agent-workspace-group').filter({ hasText: '归属工作区' }).getByRole('button', { name: '归属工作区会话', exact: true })).toHaveCount(0);

  await page.reload();
  const persistedPinnedConversation = page.getByRole('region', { name: '置顶会话' }).getByRole('button', { name: '归属工作区会话', exact: true });
  await expect(persistedPinnedConversation).toBeVisible();
  await persistedPinnedConversation.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '取消置顶' }).click();
  await expect(page.getByRole('region', { name: '置顶会话' })).toHaveCount(0);
  await expect.poll(() => pinnedWrites).toEqual([
    { id: 'sidebar-directory-pinned', pinned: true },
    { id: 'sidebar-directory-pinned', pinned: false },
  ]);
  await expect(page.locator('.agent-workspace-group').filter({ hasText: '归属工作区' }).getByRole('button', { name: '归属工作区会话', exact: true })).toBeVisible();

  const rootConversation = page.getByRole('button', { name: '未读根会话', exact: true });
  await rootConversation.click();
  await expect.poll(() => unreadWrites).toEqual([{ id: 'sidebar-root-unread', unread: false }]);
  const runningRowInWorkspaceList = page.locator('[data-conversation-binding-id="sidebar-directory-running"]');
  const runningAlertInWorkspaceList = runningRowInWorkspaceList.getByRole('img', { name: '会话正在运行但后台长时间未产生可确认进展' });
  await expect(runningAlertInWorkspaceList).toBeVisible();
  await expect(runningAlertInWorkspaceList).toHaveCSS('color', 'rgb(197, 63, 63)');
  await expect(runningAlertInWorkspaceList).toHaveClass(/running/);

  await page.getByRole('button', { name: /查看活动会话/ }).click();
  const activity = page.getByRole('region', { name: '活动会话' });
  await expect(activity).toBeVisible();
  await expect.poll(() => activity.locator('[data-conversation-binding-id]').evaluateAll(rows => rows.map(row => row.getAttribute('data-conversation-binding-id')))).toEqual([
    'sidebar-directory-running',
  ]);
  const stalledRow = activity.locator('[data-conversation-binding-id="sidebar-directory-running"]');
  const stalledAlert = stalledRow.getByRole('img', { name: '会话正在运行但后台长时间未产生可确认进展' });
  await expect(stalledAlert).toBeVisible();
  await expect(stalledAlert).toHaveCSS('color', 'rgb(197, 63, 63)');
  await expect(stalledAlert).toHaveClass(/running/);

  const runningConversation = activity.getByRole('button', { name: '运行中目标会话', exact: true });
  await runningConversation.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-root-unread$/);
  await expect(activity).toBeVisible();
  await expect(runningConversation).toHaveClass(/active/);
  await expect(page.getByText('会话 sidebar-directory-running', { exact: true })).toBeVisible();

  await runningConversation.dblclick();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-directory-running$/);
  await expect(activity).toHaveCount(0);
  const selectedRunningRow = page.locator('[data-conversation-binding-id="sidebar-directory-running"]');
  await expect(selectedRunningRow).toHaveClass(/sidebar-reveal/);
  await expect(selectedRunningRow.getByRole('img', { name: '会话正在运行但后台长时间未产生可确认进展' })).toBeVisible();
  await expect.poll(() => unreadWrites).toEqual([
    { id: 'sidebar-root-unread', unread: false },
    { id: 'sidebar-directory-running', unread: false },
  ]);

  runningConversationPossiblyStuck = false;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(selectedRunningRow.getByRole('img', { name: '会话正在运行但后台长时间未产生可确认进展' })).toHaveCount(0);
  await expect(selectedRunningRow.getByRole('img', { name: '会话正在运行' })).toBeVisible();

  await page.getByRole('button', { name: '搜索会话' }).click();
  await page.getByLabel('搜索会话内容').fill('精准定位');
  await page.getByLabel('搜索会话内容').press('Enter');
  await page.getByRole('dialog').getByRole('button', { name: /搜索目标会话/ }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/sidebar-search-target$/);
  await expect(page.locator('[data-conversation-event-id="sidebar-search-event"]')).toHaveClass(/conversation-search-target/);
});

test('Agent session exits the first-screen gate when hydration never settles', async ({ page }) => {
  let authenticated = false;
  let hydrationReads = 0;
  let releaseHydration: (() => void) | undefined;
  const delayedHydration = new Promise<void>(resolve => { releaseHydration = resolve; });
  const workspace = {
    id: 'hydration-timeout-workspace', display_name: '水合超时工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'hydration-timeout-conversation', display_title: '水合响应体超时会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
    created_at: now, updated_at: now,
  };
  const hydration = {
    events: {
      events: [
        { id: 'hydration-recovered-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '恢复读取后的问题', timestamp: now } },
        { id: 'hydration-recovered-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'hydration-recovered-user', content: '已重新读取会话。', timestamp: now } },
      ],
      next_cursor: 'hydration-recovered-agent', history_cursor: null, result: { status: 'COMPLETED' },
    },
    context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
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
    if (path.endsWith('/hydration')) {
      hydrationReads += 1;
      if (hydrationReads === 1) await delayedHydration;
      return json(route, hydration);
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
  await page.goto('/agent/conversations/hydration-timeout-conversation');
  await expect.poll(() => hydrationReads).toBe(1);
  const timeoutAlert = page.getByRole('alert');
  await expect(timeoutAlert).toContainText('会话暂时无法读取', { timeout: 14_000 });
  await expect(timeoutAlert).toContainText('读取会话超时，请重试。');
  await expect(timeoutAlert.getByRole('button', { name: '重新读取会话' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
  expect(hydrationReads).toBe(1);

  await timeoutAlert.getByRole('button', { name: '重新读取会话' }).click();
  await expect(page.getByText('已重新读取会话。', { exact: true })).toBeVisible();
  expect(releaseHydration).toBeDefined();
  releaseHydration?.();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(hydrationReads).toBe(2);
});

test('Agent session keeps background activity visible without readiness polling or preview-suppressed unread', async ({ page }) => {
  let authenticated = false;
  let backgroundRunning = true;
  let inputReadinessReads = 0;
  const activityActiveBindings: Array<string | null> = [];
  const unreadWrites: Array<{ bindingId: string; unread: boolean }> = [];
  const workspace = {
    id: 'activity-regression-workspace', display_name: '活动回归工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const foreground = {
    id: 'activity-foreground', display_title: '当前正式会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const background = {
    id: 'activity-background', display_title: '后台运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const conversations = [foreground, background];

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
    if (path.endsWith('/conversation-activity')) {
      activityActiveBindings.push(url.searchParams.get('active_binding_id'));
      return json(route, { running_binding_ids: backgroundRunning ? [background.id] : [] });
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, {
      items: conversations.map(item => item.id === background.id
        ? { ...item, execution_status: backgroundRunning ? 'running' : 'idle' }
        : item),
      next_cursor: null,
    });
    if (path.endsWith('/hydration')) {
      const bindingId = path.split('/').at(-2)!;
      return json(route, {
        events: { events: [], next_cursor: null, history_cursor: null, result: { status: bindingId === background.id && backgroundRunning ? 'RUNNING' : 'COMPLETED' } },
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
        readiness: { ready: bindingId !== background.id || !backgroundRunning, execution_status: bindingId === background.id && backgroundRunning ? 'running' : 'idle' },
      });
    }
    if (path.endsWith('/input-readiness')) {
      inputReadinessReads += 1;
      return json(route, { ready: true, execution_status: 'idle' });
    }
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null, history_cursor: null, result: { status: backgroundRunning ? 'RUNNING' : 'COMPLETED' } });
    if (path.endsWith('/unread') && request.method() === 'PUT') {
      const bindingId = path.split('/').at(-2)!;
      const body = request.postDataJSON() as { unread: boolean };
      unreadWrites.push({ bindingId, unread: body.unread });
      const conversation = conversations.find(item => item.id === bindingId)!;
      Object.assign(conversation, { unread: body.unread, unread_origin: body.unread ? 'MANUAL' : null });
      return json(route, conversation);
    }
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversations.find(item => item.id === path.split('/').at(-1)!));
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${foreground.id}`);
  const backgroundRow = page.locator(`[data-conversation-binding-id="${background.id}"]`);
  await expect(backgroundRow.getByRole('img', { name: '会话正在运行' })).toBeVisible();

  await page.getByRole('button', { name: /查看活动会话/ }).click();
  const activity = page.getByRole('region', { name: '活动会话' });
  await activity.getByRole('button', { name: background.display_title, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/agent/conversations/${foreground.id}$`));
  await expect.poll(() => activityActiveBindings.at(-1)).toBe(foreground.id);

  // A stable running hydration must not start an exact readiness polling loop.
  await page.waitForTimeout(2_500);
  expect(inputReadinessReads).toBeLessThanOrEqual(1);

  backgroundRunning = false;
  await page.waitForTimeout(2_500);
  await expect.poll(() => unreadWrites).toContainEqual({ bindingId: background.id, unread: true });
  await expect(backgroundRow.getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();
});

test('Accepted message keeps a historical conversation in creation-time order', async ({ page }) => {
  let authenticated = false;
  let accepted = false;
  const workspace = { id: 'recent-activity-workspace', display_name: '创建时间排序工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = [
    {
      id: 'recent-activity-current', display_title: '当前会话', title_state: 'MANUAL' as const,
      lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T09:30:00Z', updated_at: '2026-09-12T09:30:00Z', sort_key: '3',
    },
    {
      id: 'recent-activity-middle', display_title: '中间会话', title_state: 'MANUAL' as const,
      lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T09:20:00Z', updated_at: '2026-09-12T09:20:00Z', sort_key: '2',
    },
    {
      id: 'recent-activity-history', display_title: '历史会话', title_state: 'MANUAL' as const,
      lifecycle: 'ACTIVE' as const, streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: '2026-09-12T09:10:00Z', updated_at: '2026-09-12T09:10:00Z', sort_key: '1',
    },
  ];

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
      // Keep returning the original order. The immediate move must not wait
      // for a refreshed server list projection.
      return json(route, { items: conversations, next_cursor: null });
    }
    if (path.endsWith('/messages') && request.method() === 'POST') {
      accepted = true;
      return json(route, { accepted: true, cursor: 'recent-activity-user-event' });
    }
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'recent-activity-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
      readiness: { ready: true, execution_status: 'idle' },
    });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, {
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'idle' });
    if (path.endsWith('/context')) return json(route, { model_name: 'recent-activity-model', window_tokens: 128_000, used_tokens: 0, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, []);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      return json(route, conversations.find(item => path.endsWith(item.id)));
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/recent-activity-history');
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(composer).toBeVisible();
  await composer.fill('继续历史会话，但不要改变排序');
  await page.getByLabel('发送消息').click();
  await expect.poll(() => accepted).toBe(true);

  const rootRows = page.locator('.agent-workspace-group').filter({ hasText: '根工作区' }).locator('[data-conversation-binding-id]');
  await expect.poll(() => rootRows.evaluateAll(rows => rows.map(row => row.getAttribute('data-conversation-binding-id')))).toEqual([
    'recent-activity-current', 'recent-activity-middle', 'recent-activity-history',
  ]);
});

test('Opening an unsubmitted draft clears a running conversation presentation', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'draft-from-running-workspace', display_name: '运行会话草稿隔离', desired_state: 'RUNNING', updated_at: now };
  const runningConversation = {
    id: 'draft-from-running-conversation', display_title: '仍在运行的旧会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const runningEvents = {
    events: [
      { id: 'draft-from-running-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '仍在运行', timestamp: now } },
      { id: 'draft-from-running-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'draft-from-running-user', content: '旧会话过程', timestamp: now } },
    ], next_cursor: 'draft-from-running-thought', history_cursor: null, result: { status: 'RUNNING' },
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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [runningConversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [runningConversation.id] });
    if (path.endsWith('/hydration')) return json(route, {
      events: runningEvents,
      context: { model_name: 'draft-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) return json(route, runningEvents);
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' }, },
    });
    if (path.endsWith('/context')) return json(route, { model_name: 'draft-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'draft-provider', name: '草稿模型', connection_state: 'CONNECTED', models: [{ model_name: 'draft-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, runningConversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto('/agent/conversations/draft-from-running-conversation');
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await page.getByRole('button', { name: '在根工作区中新建会话' }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  await expect(page.getByText('正在同步 Agent 状态', { exact: true })).toHaveCount(0);
  await expect(page.getByText('正在提交消息', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
});

test('Deleting a stale conversation row treats the server-side missing binding as deleted', async ({ page }) => {
  let authenticated = false;
  let deleteRequests = 0;
  const workspace = {
    id: 'stale-delete-workspace', display_name: '删除竞态工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const conversation = {
    id: 'stale-delete-conversation', display_title: '已删除的会话', title_state: 'MANUAL',
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
    if (path.endsWith(`/conversations/${conversation.id}`) && request.method() === 'DELETE') {
      deleteRequests += 1;
      return json(route, { error: { code: 'AGENT_CONVERSATION_NOT_FOUND', message: '会话不存在或已删除' } }, 404);
    }
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } },
      context: { model_name: 'stale-delete-model', window_tokens: 128_000, used_tokens: 0, usage_current: true },
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
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${conversation.id}`);
  const conversationRow = page.locator(`[data-conversation-binding-id="${conversation.id}"]`);
  await expect(conversationRow).toBeVisible();
  await conversationRow.hover();
  await page.getByRole('button', { name: `删除会话 ${conversation.display_title}` }).click();
  await page.getByRole('button', { name: '确认删除' }).click();

  await expect.poll(() => deleteRequests).toBe(1);
  await expect(page.locator(`[data-conversation-binding-id="${conversation.id}"]`)).toHaveCount(0);
  await expect(page.getByText('会话不存在或已删除')).toHaveCount(0);
});

test('A binding confirmed missing by background event synchronization clears its transcript and stops retries', async ({ page }) => {
  let authenticated = false;
  let releaseEventsRead: (() => void) | undefined;
  let eventReads = 0;
  const eventsRead = new Promise<void>(resolve => { releaseEventsRead = resolve; });
  const workspace = {
    id: 'background-missing-workspace', display_name: '后台删除工作区', desired_state: 'RUNNING', updated_at: now,
  };
  const deleted = {
    id: 'background-missing-conversation', display_title: '已删除但仍缓存的会话', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running',
    created_at: now, updated_at: now,
  };
  const survivor = {
    id: 'background-missing-survivor', display_title: '仍存在的会话', title_state: 'MANUAL',
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
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [deleted, survivor], next_cursor: null });
    if (path.endsWith(`/conversations/${deleted.id}`) && request.method() === 'GET') return json(route, deleted);
    if (path.endsWith(`/conversations/${survivor.id}`) && request.method() === 'GET') return json(route, survivor);
    if (path.endsWith('/hydration')) return json(route, {
      events: { events: [{ id: 'deleted-message', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: '__root__', content: '这段缓存的会话正文必须消失', timestamp: now } }], next_cursor: null, history_cursor: null, result: { status: 'RUNNING' } },
      context: { model_name: 'missing-model', window_tokens: 128_000, used_tokens: 1, usage_current: true },
      readiness: { ready: false, execution_status: 'running' },
    });
    if (path.endsWith('/events')) {
      eventReads += 1;
      await eventsRead;
      return json(route, { error: { code: 'AGENT_CONVERSATION_NOT_FOUND', message: '会话不存在或已删除' } }, 404);
    }
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
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${deleted.id}`);
  await expect(page.getByText('这段缓存的会话正文必须消失')).toBeVisible();
  await expect.poll(() => eventReads).toBe(1);
  releaseEventsRead?.();
  await expect(page).not.toHaveURL(new RegExp(`/agent/conversations/${deleted.id}$`));
  await expect(page.locator(`[data-conversation-binding-id="${deleted.id}"]`)).toHaveCount(0);
  await expect(page.getByText('这段缓存的会话正文必须消失')).toHaveCount(0);
  // A first-screen query and a recovery query may already be in flight when
  // the authoritative 404 arrives. Once those settle, no delayed callback may
  // restart the deleted binding's event stream.
  await page.waitForTimeout(1_500);
  const readsAfterInflightDrain = eventReads;
  await page.waitForTimeout(2_000);
  expect(eventReads).toBe(readsAfterInflightDrain);
});


test('Pausing and resuming keeps the event stream visible without reloading', async ({ page }) => {
  let authenticated = false;
  let paused = false;
  let resumed = false;
  let stream: WebSocketRoute | undefined;
  const workspace = { id: 'pause-resume-workspace', display_name: '暂停继续工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'pause-resume-conversation', display_title: '暂停继续会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
  };
  const events = {
    events: [{ id: 'pause-resume-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '继续前的任务', timestamp: now } }],
    next_cursor: 'pause-resume-user', history_cursor: null, result: { status: 'RUNNING' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', socket => { stream = socket; });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const status = resumed ? 'running' : paused ? 'idle' : 'running';
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [{ ...conversation, execution_status: status }], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: status === 'running' ? [conversation.id] : [] });
    if (path.endsWith('/hydration')) return json(route, {
      events: { ...events, result: { status: status === 'running' ? 'RUNNING' : 'PAUSED' } },
      context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
      readiness: { ready: status !== 'running', execution_status: status },
    });
    if (path.endsWith('/interrupt') && request.method() === 'POST') { paused = true; return json(route, { accepted: true }, 202); }
    if (path.endsWith('/resume') && request.method() === 'POST') { resumed = true; return json(route, { accepted: true, cursor: 'pause-resume-user' }, 202); }
    if (path.endsWith('/events')) return json(route, { ...events, result: { status: status === 'running' ? 'RUNNING' : 'PAUSED' } });
    if (path.endsWith('/input-readiness')) return json(route, { ready: status !== 'running', execution_status: status });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, {
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
      work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await login(page);
  await page.goto(`/agent/conversations/${conversation.id}`);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  await expect.poll(() => Boolean(stream)).toBe(true);

  await page.getByRole('button', { name: '暂停当前 Agent' }).click();
  await expect(page.getByRole('button', { name: '继续当前 Agent' })).toBeVisible();

  await page.getByRole('button', { name: '继续当前 Agent' }).click();
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();
  stream!.send(JSON.stringify({
    type: 'event',
    event: { id: 'pause-resume-progress', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'pause-resume-user', content: '恢复后无需刷新即可看到这条进展。', thought: '恢复后无需刷新即可看到这条进展。', timestamp: now } },
  }));
  await expect(page.getByText('恢复后无需刷新即可看到这条进展。', { exact: true })).toBeVisible();
});
