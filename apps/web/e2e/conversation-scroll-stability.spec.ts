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

test('running transcript keeps a fixed bottom gap while formal text grows', async ({ page }) => {
  let authenticated = false;
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
    history_cursor: null,
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
    if (path.endsWith('/events')) return json(route, eventBatch);
    if (path.endsWith('/input-readiness')) return json(route, { ready: false, execution_status: 'running' });
    if (path.endsWith('/context')) return json(route, { model_name: 'scroll-model', window_tokens: 128_000, used_tokens: 2_048, usage_current: true });
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
  await page.goto('/agent/conversations/scroll-stability-conversation');
  await expect(page.getByText('初始思考内容')).toBeVisible();
  await expect.poll(() => Boolean(stream)).toBe(true);
  const surface = page.locator('.conversation-surface');
  await surface.evaluate(element => { element.scrollTop = element.scrollHeight; });

  const startPaintSampling = async () => surface.evaluate(element => {
    element.dataset.paintBottomGaps = '[]';
    let remainingFrames = 10;
    const sample = () => {
      const gaps = JSON.parse(element.dataset.paintBottomGaps ?? '[]') as number[];
      gaps.push(element.scrollHeight - element.scrollTop - element.clientHeight);
      element.dataset.paintBottomGaps = JSON.stringify(gaps);
      remainingFrames -= 1;
      if (remainingFrames > 0) requestAnimationFrame(() => window.setTimeout(sample, 0));
    };
    requestAnimationFrame(() => window.setTimeout(sample, 0));
  });
  const expectStablePaintedGap = async () => {
    await expect.poll(() => surface.evaluate(element => (
      JSON.parse(element.dataset.paintBottomGaps ?? '[]') as number[]
    ).length)).toBe(10);
    const gaps = await surface.evaluate(element => (
      JSON.parse(element.dataset.paintBottomGaps ?? '[]') as number[]
    ));
    expect(Math.max(...gaps), `painted bottom gaps: ${JSON.stringify(gaps)}`).toBeLessThanOrEqual(16);
  };

  const expandedThought = Array.from(
    { length: 24 },
    (_, index) => `正式思考扩展段 ${index + 1}：正文从首次绘制就占据最终高度。`,
  ).join('\n\n');
  await startPaintSampling();
  stream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-stability-thought', 'THOUGHT', {
      source: 'agent', parent_id: 'scroll-stability-user', content: expandedThought, thought: expandedThought,
    }),
  }));
  await expect(page.getByText(/正式思考扩展段 24/)).toBeVisible();
  await expectStablePaintedGap();

  await startPaintSampling();
  stream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-stability-tool', 'TOOL_CALL', {
      source: 'agent', parent_id: 'scroll-stability-thought', action_id: 'scroll-stability-tool',
      tool_call_id: 'scroll-stability-call', tool_name: 'terminal', event_name: 'TerminalAction',
      details: { command: 'git diff --check' },
    }),
  }));
  await expect(page.locator('.conversation-progress-current').filter({ hasText: '正在运行 git diff --check' })).toBeVisible();
  await expectStablePaintedGap();

  await surface.hover();
  const bottomPosition = await surface.evaluate(element => element.scrollTop);
  await page.mouse.wheel(0, -500);
  await expect.poll(() => surface.evaluate(element => element.scrollTop)).toBeLessThan(bottomPosition);
  await page.evaluate(() => new Promise<void>(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const readingPosition = await surface.evaluate(element => element.scrollTop);
  await expect(page.getByRole('button', { name: '跳转到正在生成的最新回复' })).toBeVisible();
  stream!.send(JSON.stringify({
    type: 'event',
    event: event('scroll-stability-tool-result', 'TOOL_RESULT', {
      source: 'environment', parent_id: 'scroll-stability-tool', action_id: 'scroll-stability-tool',
      tool_call_id: 'scroll-stability-call', tool_name: 'terminal', event_name: 'TerminalObservation',
      content: '完成', details: { command: 'git diff --check', stdout: '', exit_code: 0, is_error: false },
    }),
  }));
  await expect(page.getByTitle('已运行 git diff --check')).toHaveCount(1);
  await expect.poll(() => surface.evaluate(element => element.scrollTop)).toBe(readingPosition);
});
