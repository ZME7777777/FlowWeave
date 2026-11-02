import { expect, test, type WebSocketRoute } from '@playwright/test';

for (const node of [false, true]) {
  for (const prefetch of [false, true]) {
    test(`${node ? 'Flow Node' : 'Workspace'} restores a new background reply over an old terminal cache (${prefetch ? 'prefetched' : 'stream-only progress'})`, async ({ page }) => {
      const timestamp = '2026-10-25T03:00:00Z';
      const workspace = { id: 'background-cache-workspace', display_name: '后台恢复工作区', desired_state: 'RUNNING', updated_at: timestamp };
      const conversation = { id: 'background-cache-a', display_title: '后台恢复会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, created_at: timestamp, updated_at: timestamp };
      const other = { ...conversation, id: 'background-cache-b', display_title: '其他会话' };
      const base = node ? '/api/v1/flow-runs/background-run/node-attempts/background-attempt/agent-sessions' : `/api/v1/agent-workspaces/${workspace.id}/conversations`;
      const conversationPath = node ? `/flow-runs/background-run/nodes/background-node/attempts/background-attempt/agent-sessions/${conversation.id}` : `/agent/conversations/${conversation.id}`;
      const initial = [
        { id: 'background-old-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '上次的问题', timestamp } },
        { id: 'background-old-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'background-old-user', content: '上次已经回复', final_reply: true, timestamp } },
      ];
      const sent = { id: 'background-new-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'background-old-reply', content: '提交代码', timestamp } };
      const thought = { id: 'background-new-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: sent.id, thought: '运行中已经看到的正式过程', timestamp } };
      const reply = { id: 'background-new-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: thought.id, content: '后台完成的新回复', final_reply: true, timestamp } };
      const context = { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true };
      let accepted = false;
      let completed = false;
      let unread = false;
      let completionAnnounced = false;
      let hydrationReads = 0;
      const hydrationCursors: Array<string | null> = [];
      let socket: WebSocketRoute | undefined;
      let releaseHydration!: () => void;
      const hydrationGate = new Promise<void>(resolve => { releaseHydration = resolve; });
      const currentConversation = () => ({
        ...conversation,
        unread,
        unread_origin: unread ? 'MANUAL' : null,
        execution_status: accepted && !completed ? 'running' : 'idle',
        final_reply_preview: unread ? { event_id: reply.id, content: reply.payload.content } : null,
      });
      const batch = (latest = false) => ({
        // A REST window can trail formal WebSocket frames. Only the completion
        // read exposes the durable branch in this reproduction.
        events: latest && completed ? [...initial, sent, thought, reply] : initial,
        next_cursor: latest && completed ? reply.id : initial.at(-1)!.id,
        history_cursor: null,
        result: { status: 'COMPLETED' },
      });
      await page.routeWebSocket('**/stream', route => { socket = route; });
      await page.route('**/api/v1/**', async route => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (path.endsWith('/auth/me')) return json({ id: 'background-cache-user', username: 'cache-user', role: 'USER', is_super_admin: false });
        if (path.endsWith('/agent-workspaces/default') || path === `${base}/host`) return json(workspace);
        if (path.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: timestamp });
        if (path === base) return json({ items: [currentConversation(), other], next_cursor: null });
        if (path.endsWith('/conversation-activity') || path.endsWith('/activity')) {
          if (completed && !completionAnnounced) { unread = true; completionAnnounced = true; }
          return json({
            // The click case deliberately misses the running catalog sample.
            running_binding_ids: prefetch && accepted && !completed ? [conversation.id] : [],
            conversations: [currentConversation()],
          });
        }
        if (path.endsWith('/hydration')) {
          const isTarget = path.includes(`/${conversation.id}/`);
          const cursor = new URL(request.url()).searchParams.get('cursor');
          if (isTarget) { hydrationReads += 1; hydrationCursors.push(cursor); }
          if (isTarget && completed) await hydrationGate;
          return json({
            events: isTarget && completed && cursor
              ? { ...batch(true), events: [sent, thought, reply] }
              : batch(isTarget),
            ...(cursor ? { incremental_from: cursor } : {}),
            context, readiness: { ready: true, execution_status: 'idle' },
          });
        }
        if (path.endsWith('/messages') && request.method() === 'POST') {
          accepted = true;
          return json({ accepted: true, cursor: sent.id }, 202);
        }
        if (path.endsWith('/unread') && request.method() === 'PUT') {
          unread = request.postDataJSON().unread;
          return json(currentConversation());
        }
        if (path.endsWith('/events')) return json(batch(path.includes(`/${conversation.id}/`)));
        // Reproduce a stale native idle snapshot while the stream has activity.
        if (path.endsWith('/input-readiness')) return json({ ready: true, execution_status: 'idle' });
        if (path.endsWith('/pending-confirmation')) return json({ pending: false });
        if (path.endsWith('/context')) return json(context);
        if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
        if (path.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
        if (path.endsWith('/model-providers')) return json([{ id: 'background-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
        if (path.endsWith('/capabilities') || path.endsWith('/capability-collections') || path.endsWith('/nodes') || path.endsWith('/flows')) return json([]);
        if (path.startsWith(`${base}/`) && request.method() === 'GET') return json(path.endsWith(other.id) ? other : currentConversation());
        return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
      });

      const initialTime = Date.now();
      await page.clock.install({ time: new Date(initialTime) });
      await page.goto(conversationPath);
      await expect(page.getByText('上次已经回复', { exact: true })).toBeVisible();
      expect(hydrationReads).toBe(1);
      if (prefetch) {
        // Another page can start a turn after this tab cached its terminal
        // snapshot. No local send marker is available to invalidate it.
        accepted = true;
      } else {
        await page.getByLabel('发送 Agent 消息').fill(sent.payload.content);
        await page.getByRole('button', { name: '发送消息', exact: true }).click();
        await expect.poll(() => accepted).toBe(true);
        await expect.poll(() => Boolean(socket)).toBe(true);
        socket!.send(JSON.stringify({ type: 'event', event: sent }));
        socket!.send(JSON.stringify({ type: 'event', event: thought }));
        await expect(page.getByText(thought.payload.thought, { exact: true })).toBeVisible();
      }
      // Exceed the POST confirmation and active cache leases, while the old
      // five-minute terminal cache would still have been considered reusable.
      await page.clock.setFixedTime(new Date(initialTime + 65_000));
      await page.getByRole('button', { name: other.display_title, exact: true }).click();
      await expect(page).toHaveURL(/background-cache-b$/);
      const row = page.locator(`[data-conversation-binding-id="${conversation.id}"]`);
      if (prefetch) await expect(row.locator('.agent-workspace-conversation-running')).toBeVisible();
      completed = true;
      // Poll the catalog without a foreground/focus recovery of the target.
      await page.clock.fastForward(15_100);
      if (!prefetch) await page.getByRole('button', { name: /^查看活动会话/ }).click();
      await expect(row.getByRole('img', { name: '会话已完成，有未读回复' })).toBeVisible();

      if (prefetch) {
        await expect.poll(() => hydrationReads).toBe(2);
        const warmed = page.waitForResponse(response => response.url().includes(`/${conversation.id}/hydration`));
        releaseHydration();
        await warmed;
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        await expect(page.getByText(reply.payload.content, { exact: true })).toHaveCount(0);
        await page.getByRole('button', { name: conversation.display_title, exact: true }).click();
      } else {
        await row.getByRole('button', { name: conversation.display_title, exact: true }).dblclick();
        await expect.poll(() => hydrationReads).toBe(2);
        await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toBeVisible();
        await expect(page.getByLabel('发送 Agent 消息')).toHaveCount(0);
        await expect(page.getByText('上次已经回复', { exact: true })).toHaveCount(0);
        await expect(page.getByText(reply.payload.content, { exact: true })).toHaveCount(0);
        releaseHydration();
      }
      await expect(page.getByText(reply.payload.content, { exact: true })).toBeVisible();
      await expect(page.getByText('上次已经回复', { exact: true })).toBeVisible();
      await expect(page.getByText(sent.payload.content, { exact: true })).toHaveCount(1);
      await expect(page.getByText(thought.payload.thought, { exact: true })).toHaveCount(1);
      await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
      await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
      expect(hydrationReads).toBe(2);
      expect(hydrationCursors).toEqual([null, 'background-old-reply']);
      await expect(row.getByRole('img', { name: '会话已完成，有未读回复' })).toHaveCount(0);
      // A subsequent revisit reuses the now-complete snapshot without another
      // Runtime hydration, and its recovered formal process remains present.
      await page.getByRole('button', { name: other.display_title, exact: true }).click();
      await page.getByRole('button', { name: conversation.display_title, exact: true }).click();
      await expect(page.getByText(reply.payload.content, { exact: true })).toBeVisible();
      await expect(page.getByText(thought.payload.thought, { exact: true })).toHaveCount(1);
      expect(hydrationReads).toBe(2);
    });
  }
}
