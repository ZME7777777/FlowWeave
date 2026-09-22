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
    lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle',
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
  const composer = page.getByLabel('发送 Agent 消息');
  await expect(composer).toBeEditable();
  await page.getByRole('button', { name: '缓存会话 B', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/cache-conversation-b$/);
  await expect(composer).toBeEditable();
  await expect.poll(() => eventRequests).toBe(2);
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
});

test('Conversation context menu marks a conversation unread until it is opened again', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'unread-workspace', display_name: '未读工作区', desired_state: 'RUNNING', updated_at: now };
  const conversations = ['unread-conversation-a', 'unread-conversation-b'].map((id, index) => ({
    id, display_title: index === 0 ? '未读会话 A' : '未读会话 B', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
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
  await expect.poll(() => page.evaluate(() => localStorage.getItem('flowweave:agent-workspace-unread:agent-workspace:unread-workspace'))).toContain('unread-conversation-a');

  await conversationB.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-b$/);
  await expect(unreadMarker).toBeVisible();
  await page.reload();
  await expect(unreadMarker).toBeVisible();

  await conversationA.click();
  await expect(page).toHaveURL(/\/agent\/conversations\/unread-conversation-a$/);
  await expect(unreadMarker).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('flowweave:agent-workspace-unread:agent-workspace:unread-workspace'))).not.toContain('unread-conversation-a');
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
  await page.getByRole('button', { name: /查看活动会话（2）/ }).click();
  const activity = page.getByRole('region', { name: '活动会话' });
  await expect(activity).toBeVisible();
  await expect.poll(() => activity.locator('[data-conversation-binding-id]').evaluateAll(rows => rows.map(row => row.getAttribute('data-conversation-binding-id')))).toEqual([
    'sidebar-directory-running',
    'sidebar-root-unread',
  ]);

  await activity.getByRole('button', { name: '运行中目标会话', exact: true }).click();
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
