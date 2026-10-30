import { expect, test, type Route } from '@playwright/test';

const now = '2026-10-17T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

for (const outcome of ['success', 'failure'] as const) {
  test(`A late resume ${outcome} stays with its original conversation after switching`, async ({ page }) => {
    let authenticated = false;
    let releaseResume: (() => void) | undefined;
    const resumePending = new Promise<void>(resolve => { releaseResume = resolve; });
    const unreadWrites: string[] = [];
    const workspace = { id: 'scoped-resume-workspace', display_name: '继续隔离工作区', desired_state: 'RUNNING', updated_at: now };
    const conversations = [
      { id: 'resume-source', display_title: '等待继续的会话', execution_status: 'paused', unread: true },
      { id: 'resume-target', display_title: '另一个已完成会话', execution_status: 'idle', unread: true },
    ].map(item => ({ ...item, title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, created_at: now, updated_at: now }));
    const eventBatch = (bindingId: string) => ({
      events: [
        { id: `${bindingId}-user`, event_type: 'MESSAGE', payload: { source: 'user', content: `${bindingId} 的提问`, parent_id: '__root__', timestamp: now } },
        ...(bindingId === 'resume-target' ? [{ id: `${bindingId}-reply`, event_type: 'MESSAGE', payload: { source: 'agent', content: '另一个会话已完成。', parent_id: `${bindingId}-user`, final_reply: true, timestamp: now } }] : []),
      ],
      next_cursor: `${bindingId}-${bindingId === 'resume-target' ? 'reply' : 'user'}`, history_cursor: null,
      result: { status: bindingId === 'resume-target' ? 'COMPLETED' : 'PAUSED' },
    });
    await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
    await page.route('**/api/v1/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const bindingId = path.match(/\/conversations\/([^/]+)/)?.[1];
      const conversation = conversations.find(item => item.id === bindingId);
      if (path.endsWith('/auth/me')) return authenticated ? json(route, { id: 'resume-user', username: 'resume-user', role: 'USER', can_use_agent_sessions: true }) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
      if (path.endsWith('/auth/login')) { authenticated = true; return json(route, { id: 'resume-user', username: 'resume-user', role: 'USER', can_use_agent_sessions: true }); }
      if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
      if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
      if (path.endsWith('/conversations')) return json(route, { items: conversations, next_cursor: null });
      if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
      if (path.endsWith('/resume')) {
        await resumePending;
        return outcome === 'success'
          ? json(route, { accepted: true, cursor: `${bindingId}-user` }, 202)
          : json(route, { error: { code: 'RESUME_FAILED', message: '原会话继续失败' } }, 409);
      }
      if (path.endsWith('/unread') && conversation) {
        unreadWrites.push(conversation.id);
        conversation.unread = request.postDataJSON().unread;
        return json(route, conversation);
      }
      if (path.endsWith('/hydration') && conversation) return json(route, {
        events: eventBatch(conversation.id),
        context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true },
        readiness: { ready: true, execution_status: conversation.execution_status },
      });
      if (path.endsWith('/events') && conversation) return json(route, eventBatch(conversation.id));
      if (path.endsWith('/input-readiness') && conversation) return json(route, { ready: true, execution_status: conversation.execution_status });
      if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
      if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true });
      if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
      if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
      if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
      if (conversation && request.method() === 'GET') return json(route, conversation);
      return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
    });
    await page.goto('/');
    await page.getByLabel('账号').fill('resume-user');
    await page.getByLabel('密码').fill('test-password');
    await page.getByRole('button', { name: '进入工作空间' }).click();
    await page.goto('/agent/conversations/resume-source');
    await page.getByRole('button', { name: '继续当前 Agent' }).click();
    await page.getByRole('button', { name: '另一个已完成会话', exact: true }).click();
    await expect(page.getByText('另一个会话已完成。', { exact: true })).toBeVisible();
    await expect.poll(() => unreadWrites).toEqual(['resume-target']);
    unreadWrites.length = 0;
    const response = page.waitForResponse(value => value.url().endsWith('/resume'));
    releaseResume!();
    await response;
    if (outcome === 'success') await expect.poll(() => unreadWrites).toEqual(['resume-source']);
    else {
      // Observe the completed network callback before asserting that the new
      // selection has retained its terminal controls and error projection.
      await page.waitForTimeout(300);
      await expect(page.getByText('原会话继续失败', { exact: true })).toHaveCount(0);
      expect(unreadWrites).toEqual([]);
    }
    await expect(page.getByRole('button', { name: '继续当前 Agent' })).toHaveCount(0);
    const target = page.locator('[data-conversation-binding-id="resume-target"]');
    await expect(target.locator('.agent-workspace-conversation-unread')).toHaveCount(0);
    await expect(target.locator('.agent-workspace-conversation-running')).toHaveCount(0);
  });
}

test('Agent navigation restores the last standalone conversation after leaving the page', async ({ page }) => {
  let authenticated = false;
  const workspace = { id: 'navigation-restore-workspace', display_name: '导航恢复工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'navigation-restore-conversation', display_title: '切换后恢复的会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
  };
  const hydration = {
    events: {
      events: [
        { id: 'navigation-restore-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '新会话已经发送消息', timestamp: now } },
        { id: 'navigation-restore-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'navigation-restore-user', content: '切换页面后仍应看到这条回复。', final_reply: true, timestamp: now } },
      ],
      next_cursor: 'navigation-restore-agent', history_cursor: null, result: { status: 'COMPLETED' },
    },
    context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true },
    readiness: { ready: true, execution_status: 'idle' },
  };

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, { id: 'navigation-restore-user', username: 'navigation-restore-user', role: 'USER', can_use_agent_sessions: true })
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) {
      authenticated = true;
      return json(route, { id: 'navigation-restore-user', username: 'navigation-restore-user', role: 'USER', can_use_agent_sessions: true });
    }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/hydration')) return json(route, hydration);
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByLabel('账号').fill('navigation-restore-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto('/agent/conversations/navigation-restore-conversation');
  await expect(page.getByText('切换页面后仍应看到这条回复。')).toBeVisible();

  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '认证管理', exact: true }).click();
  await expect(page.getByRole('button', { name: '认证管理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/navigation-restore-conversation$/);
  await expect(page.getByText('切换页面后仍应看到这条回复。')).toBeVisible();

  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/navigation-restore-conversation$/);
  await expect(page.getByText('切换页面后仍应看到这条回复。')).toBeVisible();

  await page.getByRole('button', { name: '在根工作区中新建会话' }).click({ force: true });
  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.locator('h2.agent-session-title')).toHaveText('新会话');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent$/);
});
