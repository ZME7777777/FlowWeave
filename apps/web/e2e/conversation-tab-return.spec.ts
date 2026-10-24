import { expect, test, type WebSocketRoute } from '@playwright/test';

for (const node of [false, true]) {
  for (const timing of ['pending-on-return', 'accepted-while-away', 'accepted-before-leaving'] as const) {
    test(`${node ? 'Flow Node' : 'Workspace'}: top navigation restores a sent message with ${timing}`, async ({ page }) => {
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
      let hydrationCount = 0;
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
          hydrationCount += 1;
          return json({ events: batch(), context, readiness: { ready: true, execution_status: 'idle' } });
        }
        if (path.endsWith('/messages') && request.method() === 'POST') {
          postCount += 1;
          sent.payload.timestamp = new Date().toISOString();
          await sendGate;
          accepted = true;
          return json({ accepted: true, cursor: sent.id }, 202);
        }
        if (path.endsWith('/events')) return json(batch(path.includes(`/${conversation.id}/`)));
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
      if (timing === 'accepted-before-leaving') {
        releaseSend();
        await expect(page.locator(`[data-user-event-id="${sent.id}"]`)).toHaveCount(1);
      }
      const nav = page.getByRole('navigation', { name: '主导航', exact: true });
      await page.getByRole('button', { name: '节点中心', exact: true }).click();
      await expect(page.getByLabel('发送 Agent 消息')).toHaveCount(0);
      if (timing === 'accepted-while-away') {
        releaseSend();
        await expect.poll(() => accepted).toBe(true);
      }
      if (node) await page.locator('.context-tab-select').click();
      else await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
      await expect.poll(() => hydrationCount).toBe(1);
      await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
      await expect(page.getByText(sent.payload.content, { exact: true })).toBeVisible({ timeout: 3_000 });
      await page.locator('.conversation-message.user').filter({ hasText: sent.payload.content }).evaluate(element => { (element as HTMLElement).dataset.returnMarker = 'same-bubble'; });
      await expect(page.getByText('正在提交消息', { exact: true })).toBeVisible();
      if (timing === 'pending-on-return') releaseSend();
      await expect(page.locator(`[data-user-event-id="${sent.id}"]`)).toHaveCount(1);
      // An old component's ACK must wake the newly mounted workbench. Neither
      // its optimistic bubble nor its formal replacement may be duplicated.
      latestAvailable = true;
      await expect(page.getByText('返回后恢复的正式过程', { exact: true })).toBeVisible();
      await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(1);
      await expect(page.locator('[data-return-marker="same-bubble"]')).toHaveCount(1);
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
      // Once REST has confirmed the event, a later formal window owns history.
      // Reusing a local bubble must not resurrect a turn absent from that window.
      const readsBeforeLeaving = hydrationCount;
      await page.getByRole('button', { name: '节点中心', exact: true }).click();
      await expect(page.getByLabel('发送 Agent 消息')).toHaveCount(0);
      if (node) await page.locator('.context-tab-select').click();
      else await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
      await expect.poll(() => hydrationCount).toBe(readsBeforeLeaving);
      await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
      await expect(page.getByText('上次已经回复', { exact: true })).toBeVisible();
      await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(1);
      expect(postCount).toBe(1);
    });
  }
}
