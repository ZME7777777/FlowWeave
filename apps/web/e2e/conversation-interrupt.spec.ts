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

for (const formalReadsFail of [false, true]) {
  test(`Explicit pause reaches interrupt while formal reads ${formalReadsFail ? 'fail with 503' : 'receive new running events'}`, async ({ page }) => {
    let authenticated = false;
    let paused = false;
    const workspace = { id: 'pause-resume-workspace', display_name: '暂停继续工作区', desired_state: 'RUNNING', updated_at: now };
    const conversation = {
      id: 'pause-resume-conversation', display_title: '暂停继续会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now,
    };
    const events = {
      events: [{ id: 'pause-resume-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '继续前的任务', timestamp: now } }],
      next_cursor: 'pause-resume-user', history_cursor: null, result: { status: 'RUNNING' },
    };

    await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
    await page.route('**/api/v1/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const status = paused ? 'paused' : 'running';
      if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
      if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return json(route, user); }
      if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
      if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
      if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [{ ...conversation, execution_status: status }], next_cursor: null });
      // The activity catalog may lag the formal pause readiness transition.
      if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [conversation.id] });
      if (path.endsWith('/hydration')) return json(route, {
        events: { ...events, result: { status: status === 'running' ? 'RUNNING' : 'PAUSED' }, monitoring: { possibly_stuck: true } },
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true },
        readiness: { ready: status !== 'running', execution_status: status },
      });
      if (path.endsWith('/interrupt') && request.method() === 'POST') { paused = true; return json(route, { accepted: true }, 202); }
      if (formalReadsFail && (path.endsWith('/events') || path.endsWith('/input-readiness'))) return json(route, { error: { code: 'RUNTIME_READ_PER_RUNTIME_SATURATED', message: '读取繁忙' } }, 503);
      if (path.endsWith('/events')) return json(route, { ...events, events: [...events.events, ...(new URL(request.url()).searchParams.get('diagnostic_trigger') === 'pause_preflight' ? [{ id: 'new-process-event', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'pause-resume-user', content: '命令仍在执行', timestamp: now } }] : [])], result: { status: status === 'running' ? 'RUNNING' : 'PAUSED' }, monitoring: { possibly_stuck: true } });
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

    await page.getByRole('button', { name: '暂停当前 Agent' }).click();
    await expect.poll(() => paused).toBe(true);
  });

}

for (const outcome of ['success', 'failure'] as const) {
  test(`A late pause ${outcome} cannot change the next conversation`, async ({ page }) => {
    test.setTimeout(30_000);
    let authenticated = false;
    let pausePosts = 0;
    let releasePause: (() => void) | undefined;
    const pauseGate = new Promise<void>(resolve => { releasePause = resolve; });
    const workspace = { id: 'pause-scope-workspace', display_name: '暂停隔离工作区', desired_state: 'RUNNING', updated_at: now };
    const first = { id: 'pause-scope-first', display_title: '原运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'running', created_at: now, updated_at: now };
    const second = { ...first, id: 'pause-scope-second', display_title: '另一空闲会话', execution_status: 'idle' };
    await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
    await page.route('**/api/v1/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const item = path.includes(second.id) ? second : first;
      const running = item.id === first.id;
      const events = { events: running ? [{ id: 'pause-scope-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '正在执行的任务', timestamp: now } }] : [], next_cursor: running ? 'pause-scope-user' : null, history_cursor: null, result: { status: running ? 'RUNNING' : 'COMPLETED' } };
      if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
      if (path.endsWith('/auth/login')) { authenticated = true; return json(route, user); }
      if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
      if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
      if (path.endsWith('/conversations')) return json(route, { items: [first, second], next_cursor: null });
      if (path.endsWith('/conversation-activity')) return json(route, { conversations: [first], running_binding_ids: [first.id] });
      if (path.endsWith('/hydration')) return json(route, { events, context: {}, readiness: { ready: !running, execution_status: item.execution_status } });
      if (path.endsWith('/interrupt')) {
        expect(path).toContain(first.id);
        pausePosts += 1;
        await pauseGate;
        return outcome === 'success' ? json(route, { accepted: true }, 202) : json(route, { error: { code: 'AGENT_RUNTIME_UNAVAILABLE', message: '原会话暂停失败的独立提示' } }, 503);
      }
      if (path.endsWith('/events')) return json(route, events);
      if (path.endsWith('/input-readiness')) return json(route, { ready: !running, execution_status: item.execution_status });
      if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
      if (path.endsWith('/context')) return json(route, {});
      if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
      if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false } } });
      if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
      if (path.includes('/conversations/')) return json(route, item);
      return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
    });
    try {
      await page.goto('/');
      await login(page);
      await page.goto(`/agent/conversations/${first.id}`);
      await page.getByRole('button', { name: '暂停当前 Agent' }).click();
      await expect.poll(() => pausePosts).toBe(1);
      await page.locator(`[data-conversation-binding-id="${second.id}"]`).getByRole('button').first().click();
      await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
      await expect(page.getByRole('button', { name: '暂停请求已发送' })).toHaveCount(0);
      const completed = page.waitForResponse(response => response.url().endsWith('/interrupt'));
      releasePause?.();
      await completed;
      await expect(page.getByText('原会话暂停失败的独立提示')).toHaveCount(0);
      await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
      await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
    } finally {
      releasePause?.();
    }
  });
}
