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

test('Agent session renders a completed long Markdown reply without manual expansion', async ({ page }) => {
  let authenticated = false;
  let eventRequests = 0;
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
      { id: `${id}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `问题 ${id}\n第二行 ${id}`, timestamp: now } },
      { id: `${id}-tool`, event_type: 'TOOL_RESULT', payload: { parent_id: `${id}-user`, content: 'x'.repeat(20_000), details: { stdout: 'x'.repeat(20_000) }, timestamp: now } },
      { id: `${id}-assistant`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${id}-tool`, content: `完整回复 ${id}\n\n| 选择 | 项目 | 用途 | Git 地址 |\n| --- | --- | --- | --- |\n| #1 | \`hq-support\` | 同步 Kafka topic | \`https://gitlab.example.test/hq-support\` |\n\n${'完整 Markdown 内容 '.repeat(500)}`, timestamp: now } },
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
  await expect(page.getByRole('button', { name: '渲染完整消息' })).toHaveCount(0);
  await expect.poll(() => eventRequests).toBe(1);
});

test('Agent composer retains each conversation draft and uploaded attachment across navigation and reload', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'draft-workspace', display_name: '草稿工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['draft-conversation-a', 'draft-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '草稿会话 A' : '草稿会话 B', title_state: 'MANUAL',
    lifecycle: 'ACTIVE', streaming_callback_ready: true, execution_status: 'idle', created_at: now, updated_at: now,
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

  await page.getByRole('button', { name: '草稿会话 B 可继续会话' }).click();
  await expect(composer).toHaveValue('');
  await composer.fill('会话 B 的未发送内容');
  await page.getByRole('button', { name: '草稿会话 A 可继续会话' }).click();
  await expect(composer).toHaveValue('会话 A 的未发送内容');
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toBeVisible();

  await page.reload();
  await expect(composer).toHaveValue('会话 A 的未发送内容');
  await expect(page.locator('.agent-composer .agent-attachments').getByText('保留附件.txt', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '草稿会话 B 可继续会话' }).click();
  await expect(composer).toHaveValue('会话 B 的未发送内容');
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
