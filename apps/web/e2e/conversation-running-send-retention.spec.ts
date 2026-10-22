import { expect, test, type WebSocketRoute } from '@playwright/test';

for (const timing of ['stream-ahead', 'ack-ahead', 'overlapping-ack', 'reversed-ack', 'external-stream'] as const) {
  test(`Running sends retain prior process with ${timing}`, async ({ page }) => {
    const timestamp = '2026-10-22T10:00:00Z';
    const user = { id: 'retention-user', username: 'retention-user', role: 'USER', is_super_admin: false };
    const workspace = { id: 'retention-workspace', display_name: '运行中消息回归', desired_state: 'RUNNING', updated_at: timestamp };
    const conversation = {
      id: 'retention-conversation', display_title: '运行中消息回归', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, created_at: timestamp, updated_at: timestamp,
    };
    const initial = [
      { id: 'initial-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '最初的问题', timestamp } },
      { id: 'initial-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'initial-user', content: '之前的回复', timestamp } },
    ];
    const activeUser = { id: 'active-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: timing === 'external-stream' ? 'initial-user' : 'initial-reply', content: '开始长任务', timestamp: '2026-10-22T10:01:00Z' } };
    const process = Array.from({ length: 6 }, (_, index) => ({
      id: `active-thought-${index}`, event_type: 'THOUGHT', payload: {
        source: 'agent', parent_id: index ? `active-thought-${index - 1}` : activeUser.id,
        content: `长任务已执行步骤 ${index + 1}`, thought: `长任务已执行步骤 ${index + 1}`,
        timestamp: `2026-10-22T10:01:0${index + 1}Z`,
      },
    }));
    const followUp = { id: 'follow-up-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: process.at(-1)!.id, content: '运行中补充要求', timestamp: '2026-10-22T10:02:00Z' } };
    const followUpThought = { id: 'follow-up-thought', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: followUp.id, content: '处理补充要求', thought: '处理补充要求', timestamp: '2026-10-22T10:02:01Z' } };
    let authenticated = false;
    let socket: WebSocketRoute | undefined;
    let postCount = 0;
    let releaseFirst: (() => void) | undefined;
    const firstAcceptance = new Promise<void>(resolve => { releaseFirst = resolve; });
    let releaseFollowUp: (() => void) | undefined;
    const acceptance = new Promise<void>(resolve => { releaseFollowUp = resolve; });
    // The REST window deliberately trails the stream throughout both sends.
    const externallyRunning = timing === 'external-stream';
    const batch = { events: externallyRunning ? initial.slice(0, 1) : initial, next_cursor: externallyRunning ? 'initial-user' : 'initial-reply', history_cursor: null, result: { status: externallyRunning ? 'RUNNING' : 'COMPLETED' } };
    const context = { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true };
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
      if (path.endsWith('/conversation-activity')) return json({ running_binding_ids: postCount || externallyRunning ? [conversation.id] : [], items: [] });
      if (path.endsWith('/hydration')) return json({ events: batch, context, readiness: { ready: !externallyRunning, execution_status: externallyRunning ? 'running' : 'idle' } });
      if (path.endsWith('/messages') && request.method() === 'POST') {
        postCount += 1;
        if (postCount === 1 && !externallyRunning) {
          activeUser.payload.timestamp = new Date().toISOString();
          if (timing === 'overlapping-ack' || timing === 'reversed-ack') await firstAcceptance;
          return json({ accepted: true, cursor: activeUser.id }, 202);
        }
        followUp.payload.timestamp = new Date().toISOString();
        await acceptance;
        return json({ accepted: true, cursor: followUp.id }, 202);
      }
      if (path.endsWith('/events')) return json(batch);
      if (path.endsWith('/input-readiness')) return json({ ready: !postCount && !externallyRunning, execution_status: postCount || externallyRunning ? 'running' : 'idle' });
      if (path.endsWith('/pending-confirmation')) return json({ pending: false });
      if (path.endsWith('/context')) return json(context);
      if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
      if (path.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
      if (path.endsWith('/model-providers')) return json([{ id: 'retention-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
      if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json([]);
      if (path.includes('/conversations/') && request.method() === 'GET') return json(conversation);
      return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
    });

    await page.goto('/');
    await page.getByLabel('账号').fill(user.username);
    await page.getByLabel('密码').fill('test-password');
    await page.getByRole('button', { name: '进入工作空间' }).click();
    await page.goto(`/agent/conversations/${conversation.id}`);
    await expect(page.getByText(externallyRunning ? '最初的问题' : '之前的回复', { exact: true })).toBeVisible();
    const composer = page.getByLabel('发送 Agent 消息');
    if (!externallyRunning) {
      await composer.fill('开始长任务');
      await page.getByRole('button', { name: '发送消息', exact: true }).click();
      await expect.poll(() => postCount).toBe(1);
    }
    await expect.poll(() => Boolean(socket)).toBe(true);
    const activeTurn = page.locator('[data-conversation-turn="active-user"]');
    if (timing !== 'overlapping-ack' && timing !== 'reversed-ack' && !externallyRunning) await expect(activeTurn).toHaveCount(1);
    if (timing === 'stream-ahead' || externallyRunning) {
      for (const event of [activeUser, ...process]) socket!.send(JSON.stringify({ type: 'event', event }));
      await expect(activeTurn.locator('summary').first()).toContainText('6 项');
      await expect(activeTurn.getByText('长任务已执行步骤 6', { exact: true })).toBeVisible();
      await activeTurn.locator('[data-user-event-id="active-user"]').evaluate(element => { (element as HTMLElement).dataset.retentionMarker = 'original'; });
    }

    await composer.fill('运行中补充要求');
    await composer.press('Alt+Enter');
    await expect.poll(() => postCount).toBe(externallyRunning ? 1 : 2);
    // A new pending send must not hide a previously displayed user subtree just
    // because that subtree has not reached the REST window yet.
    if (timing !== 'stream-ahead' && !externallyRunning) {
      for (const event of [activeUser, ...process]) socket!.send(JSON.stringify({ type: 'event', event }));
      if (timing === 'overlapping-ack') {
        // The earlier branch waits for its own cursor, then becomes visible even
        // while the newer submission is still waiting for acknowledgement.
        await expect(page.getByText('长任务已执行步骤 6', { exact: true })).toHaveCount(0);
        releaseFirst?.();
      }
    }
    if (timing !== 'reversed-ack') {
      await expect(activeTurn.locator('summary').first()).toContainText('6 项');
      for (const event of process) await expect(activeTurn.getByText(event.payload.content, { exact: true })).toBeAttached();
    }
    for (const event of [followUp, followUpThought]) socket!.send(JSON.stringify({ type: 'event', event }));
    if (timing !== 'reversed-ack') await expect(activeTurn.locator('summary').first()).toContainText('6 项');
    await expect(page.getByText('处理补充要求', { exact: true })).toHaveCount(0);
    await expect(page.getByText('运行中补充要求', { exact: true })).toHaveCount(1);
    releaseFollowUp?.();
    await expect(page.locator('[data-conversation-turn="follow-up-user"]').getByText('处理补充要求', { exact: true })).toBeVisible();
    if (timing === 'reversed-ack') releaseFirst?.();
    await expect(activeTurn.locator('summary').first()).toContainText('6 项');
    if (timing === 'stream-ahead' || externallyRunning) await expect(activeTurn.locator('[data-retention-marker="original"]')).toHaveCount(1);
    await expect(page.locator('.conversation-message.user')).toHaveCount(3);
    expect(postCount).toBe(externallyRunning ? 1 : 2);
  });
}
