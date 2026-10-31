import { expect, test, type WebSocketRoute } from '@playwright/test';

for (const node of [false, true]) {
  for (const timing of ['stay', 'switch'] as const) {
    test(`${node ? 'Flow Node' : 'Workspace'}: accepted message survives a stale formal window with ${timing}`, async ({ page }) => {
      const timestamp = '2026-10-22T10:00:00Z';
      const workspace = { id: 'tab-return-workspace', display_name: '标签恢复工作区', desired_state: 'RUNNING', updated_at: timestamp };
      const conversation = { id: 'tab-return-a', display_title: '标签恢复会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, created_at: timestamp, updated_at: timestamp };
      const other = { ...conversation, id: 'tab-return-b', display_title: '其他会话' };
      const base = node ? '/api/v1/flow-runs/tab-run/node-attempts/tab-attempt/agent-sessions' : `/api/v1/agent-workspaces/${workspace.id}/conversations`;
      const conversationPath = node ? `/flow-runs/tab-run/nodes/tab-node/attempts/tab-attempt/agent-sessions/${conversation.id}` : `/agent/conversations/${conversation.id}`;
      const initial = [
        { id: 'old-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '上次的问题', timestamp } },
        { id: 'old-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'old-user', content: '上次已经回复', timestamp } },
      ];
      const sent = { id: 'new-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'old-reply', content: '刚发送还没有回复的消息', timestamp } };
      const thought = { id: 'new-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: sent.id, thought: '返回后恢复的正式过程', timestamp } };
      const reply = { id: 'new-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: thought.id, content: '返回后恢复的正式回复', timestamp } };
      const context = { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true };
      let postCount = 0;

      let eventReads = 0;
      let accepted = false;
      let finished = false;
      let latestAvailable = false;
      let socket: WebSocketRoute | undefined;
      let releaseSend!: () => void;
      const sendGate = new Promise<void>(resolve => { releaseSend = resolve; });
      const batch = (latest = false) => ({
        events: latest && latestAvailable ? [...initial, sent, thought, ...(finished ? [reply] : [])] : initial,
        next_cursor: latest && latestAvailable ? (finished ? reply.id : thought.id) : 'old-reply',
        history_cursor: null, result: { status: latest && latestAvailable && !finished ? 'RUNNING' : 'COMPLETED' },
      });
      await page.routeWebSocket('**/stream', route => { socket = route; });
      await page.route('**/api/v1/**', async route => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (path.endsWith('/auth/me')) return json({ id: 'tab-return-user', username: 'tab-return-user', role: 'USER', is_super_admin: false });
        if (path.endsWith('/agent-workspaces/default') || path === `${base}/host`) return json(workspace);
        if (path.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: timestamp });
        if (path === base) return json({ items: [conversation, other], next_cursor: null });
        // Keep catalog/activity and reentry hydration behind the send, as can
        // happen when a read starts before native message persistence completes.
        if (path.endsWith('/conversation-activity') || path.endsWith('/activity')) return json({ running_binding_ids: [], items: [] });
        if (path.endsWith('/hydration')) {

          return json({ events: batch(), context, readiness: { ready: true, execution_status: 'idle' } });
        }
        if (path.endsWith('/messages') && request.method() === 'POST') {
          postCount += 1;
          sent.payload.timestamp = new Date().toISOString();
          await sendGate;
          accepted = true;
          return json({ accepted: true, cursor: sent.id }, 202);
        }
        if (path.endsWith('/events')) { eventReads += 1; return json(batch(path.includes(`/${conversation.id}/`))); }
        if (path.endsWith('/input-readiness')) return json({ ready: !accepted || finished, execution_status: accepted && !finished ? 'running' : 'idle' });
        if (path.endsWith('/pending-confirmation')) return json({ pending: false });
        if (path.endsWith('/context')) return json(context);
        if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
        if (path.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
        if (path.endsWith('/model-providers')) return json([{ id: 'tab-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
        if (path.endsWith('/capabilities') || path.endsWith('/capability-collections') || path.endsWith('/nodes') || path.endsWith('/flows')) return json([]);
        if (path.startsWith(`${base}/`) && request.method() === 'GET') return json(path.endsWith(other.id) ? other : conversation);
        return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
      });
      await page.goto(conversationPath);
      await expect(page.getByText('上次已经回复', { exact: true })).toBeVisible();
      await page.getByLabel('发送 Agent 消息').fill(sent.payload.content);
      await page.getByRole('button', { name: '发送消息', exact: true }).click();
      await expect.poll(() => postCount).toBe(1);
      await expect(page.getByText(sent.payload.content, { exact: true })).toBeVisible();
      await page.locator('.conversation-message.user').filter({ hasText: sent.payload.content }).evaluate(element => { (element as HTMLElement).dataset.returnMarker = 'same-bubble'; });
      releaseSend();
      await expect(page.locator(`[data-user-event-id="${sent.id}"]`)).toHaveCount(1);
      const reads = eventReads;
      await expect.poll(() => eventReads, { timeout: 10_000 }).toBeGreaterThan(reads);
      await expect(page.getByText('上次的问题', { exact: true })).toBeVisible();
      await expect(page.getByText('上次已经回复', { exact: true })).toBeVisible();
      await expect(page.getByText(sent.payload.content, { exact: true })).toBeVisible();
      await expect(page.getByText('正在提交消息', { exact: true })).toBeVisible();
      await expect(page.locator('.operation-error')).toHaveCount(0);
      if (timing === 'switch') {
        await page.getByRole('button', { name: other.display_title, exact: true }).click();
        await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(0);
        await page.getByRole('button', { name: conversation.display_title, exact: true }).click();
        await expect(page.getByText(sent.payload.content, { exact: true })).toBeVisible();
        await expect(page.getByText('正在提交消息', { exact: true })).toBeVisible();
      }
      latestAvailable = true;
      await expect(page.getByText('返回后恢复的正式过程', { exact: true })).toBeVisible();
      await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(1);
      if (timing === 'stay') await expect(page.locator('[data-return-marker="same-bubble"]')).toHaveCount(1);
      await expect(page.getByText('正在提交消息', { exact: true })).toHaveCount(0);
      await page.getByRole('button', { name: other.display_title, exact: true }).click();
      await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(0);
      await page.getByRole('button', { name: conversation.display_title, exact: true }).click();
      await expect(page.getByText(sent.payload.content, { exact: true })).toBeVisible();
      finished = true;
      socket?.send(JSON.stringify({ type: 'message_complete' }));
      await expect(page.getByText(reply.payload.content, { exact: true })).toBeVisible();
      await page.getByLabel('发送 Agent 消息').fill('下一条尚未发送的草稿');
      await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
      expect(postCount).toBe(1);
    });
  }
}


test('message completion unlocks the current composer before terminal REST hydration', async ({ page }) => {
  const timestamp = '2026-10-31T02:00:00Z';
  const user = { id: 'message-complete-user', username: 'message-complete-user', role: 'USER', is_super_admin: false };
  const workspace = { id: 'message-complete-workspace', display_name: '完成信号工作区', desired_state: 'RUNNING', updated_at: timestamp };
  const conversation = { id: 'message-complete-conversation', display_title: '完成信号会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, created_at: timestamp, updated_at: timestamp };
  const initial = [
    { id: 'message-complete-user-event', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '已完成但尚未同步', timestamp } },
    { id: 'message-complete-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'message-complete-user-event', thought: '正在整理最终回复', timestamp } },
  ];
  const context = { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true };
  let authenticated = false;
  let posts = 0;
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/agent-workspaces/**/stream', route => { socket = route; });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return authenticated ? json(user) : json({ error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(user); }
    if (path.endsWith('/agent-workspaces/default')) return json(workspace);
    if (path.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: timestamp });
    if (path.endsWith('/conversations')) return json({ items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json({ running_binding_ids: [conversation.id], items: [] });
    if (path.endsWith('/hydration')) return json({ events: { events: initial, next_cursor: 'message-complete-thought', history_cursor: null, result: { status: 'RUNNING' } }, context, readiness: { ready: false, execution_status: 'running' } });
    if (path.endsWith('/events')) return json({ events: initial, next_cursor: 'message-complete-thought', history_cursor: null, result: { status: 'RUNNING' } });
    if (path.endsWith('/input-readiness')) return json({ ready: false, execution_status: 'running' });
    if (path.endsWith('/messages') && request.method() === 'POST') { posts += 1; return json({ accepted: true, cursor: `message-complete-next-${posts}` }, 202); }
    if (path.endsWith('/pending-confirmation')) return json({ pending: false });
    if (path.endsWith('/context')) return json(context);
    if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/model-providers')) return json([{ id: 'message-complete-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json([]);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(conversation);
    return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByLabel('账号').fill(user.username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto(`/agent/conversations/${conversation.id}`);
  await expect.poll(() => Boolean(socket)).toBe(true);
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toBeVisible();

  socket!.send(JSON.stringify({ type: 'message_complete' }));
  await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
  await expect(page.locator(`[data-conversation-binding-id="${conversation.id}"] .agent-workspace-conversation-running`)).toHaveCount(0);
  await page.getByLabel('发送 Agent 消息').fill('完成后立即发送');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(() => posts).toBe(1);
  await expect(page.getByLabel('消息投递队列')).toHaveCount(0);
});
