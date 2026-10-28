import { expect, test, type WebSocketRoute } from '@playwright/test';
import { projectHistoricalAgentReplies } from '../src/components/conversationEvents';
import type { OpenHandsConversationEvent } from '../src/types';

const timestamp = '2026-10-12T10:00:00Z';

for (const { transport, delivery } of ['stream-before-ack', 'rest-before-ack', 'rest-after-ack'].flatMap(transport => (
  ['direct', 'queued', 'queued-guidance'].map(delivery => ({ transport, delivery }))
))) {
  test(`Existing conversation keeps formal message ancestry through ${delivery} ${transport}`, async ({ page }) => {
    let authenticated = false;
    let socket: WebSocketRoute | undefined;
    let releaseAcceptance: (() => void) | undefined;
    let submitted = false;
    let finished = false;
    let showFormalUser = false;
    let postCount = 0;
    let eventReads = 0;
    let previousFinished = delivery === 'direct';
    const acceptance = new Promise<void>(resolve => { releaseAcceptance = resolve; });
    const user = { id: 'identity-user', username: 'identity-user', role: 'USER', is_super_admin: false };
    const workspace = { id: 'identity-workspace', display_name: '身份回归工作区', desired_state: 'RUNNING', updated_at: timestamp };
    const conversation = {
      id: 'identity-conversation', display_title: '已有会话身份回归', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: timestamp, updated_at: timestamp,
    };
    const initial = [
      { id: 'previous-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '最初的消息', timestamp } },
      { id: 'previous-action', event_type: 'TOOL_CALL', payload: { parent_id: 'previous-user', action_id: 'previous-action', tool_call_id: 'previous-tool', tool_name: 'terminal', event_name: 'TerminalAction', details: { command: 'pwd' }, timestamp } },
      { id: 'previous-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'previous-action', action_id: 'previous-action', tool_call_id: 'previous-tool', tool_name: 'terminal', event_name: 'TerminalObservation', details: { exit_code: 0 }, timestamp } },
      { id: 'previous-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'previous-result', content: '上一轮已经完成。', timestamp } },
    ];
    const previousReply = initial.at(-1)!;
    const current = [
      { id: 'formal-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'previous-reply', content: '刚刚发送的消息', timestamp } },
      { id: 'formal-action', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'formal-user', action_id: 'formal-action', tool_call_id: 'identity-tool', event_name: 'TerminalAction', tool_name: 'terminal', content: '本轮正在读取包配置。', details: { command: 'cat package.json' }, timestamp } },
      { id: 'formal-result', event_type: 'TOOL_RESULT', payload: { source: 'environment', parent_id: 'formal-action', action_id: 'formal-action', tool_call_id: 'identity-tool', event_name: 'TerminalObservation', content: 'pnpm', details: { exit_code: 0 }, timestamp } },
    ];
    const reply = { id: 'formal-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'formal-result', content: '本轮最终回复已经完成。', timestamp } };
    const batch = (hydration = false) => ({
      // A bounded recovery window can start after the preceding reply once
      // the queued message has advanced HEAD. Reload still sees both turns.
      events: [...initial.filter(event => (previousFinished || event.id !== 'previous-reply')
        && (hydration || delivery !== 'queued-guidance' || !submitted)), ...(showFormalUser ? current : []), ...(finished ? [reply] : [])],
      next_cursor: finished ? reply.id : showFormalUser ? 'formal-result' : previousFinished ? 'previous-reply' : 'previous-result',
      history_cursor: null,
      result: { status: finished || (!submitted && previousFinished) ? 'COMPLETED' : 'RUNNING' },
    });
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
      if (path.endsWith('/conversation-activity')) return json({ running_binding_ids: submitted && !finished ? [conversation.id] : [], items: [] });
      if (path.endsWith('/hydration')) return json({
        events: batch(true), context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true },
        readiness: { ready: previousFinished && (!submitted || finished), execution_status: !previousFinished || (submitted && !finished) ? 'running' : 'idle' },
      });
      if (path.endsWith('/messages') && request.method() === 'POST') {
        postCount += 1;
        submitted = true;
        await acceptance;
        return json({ accepted: true, cursor: 'formal-user' }, 202);
      }
      if (path.endsWith('/events')) { eventReads += 1; return json(batch()); }
      // Retain the preceding idle snapshot to exercise the submission bridge.
      if (path.endsWith('/input-readiness')) return json({ ready: previousFinished, execution_status: previousFinished ? 'idle' : 'running' });
      if (path.endsWith('/pending-confirmation')) return json({ pending: false });
      if (path.endsWith('/context')) return json({ model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true });
      if (path.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
      if (path.endsWith('/workspace')) return json({
        root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
        work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
      });
      if (path.endsWith('/model-providers')) return json([{ id: 'identity-provider', name: '测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'test-model', enabled: true, is_default: true }] }]);
      if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json([]);
      if (path.includes('/conversations/') && request.method() === 'GET') return json(conversation);
      return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
    });

    await page.goto('/');
    await page.getByLabel('账号').fill(user.username);
    await page.getByLabel('密码').fill('test-password');
    await page.getByRole('button', { name: '进入工作空间' }).click();
    await page.goto(`/agent/conversations/${conversation.id}`);
    await expect(page.getByText('最初的消息', { exact: true })).toBeVisible();
    if (delivery === 'direct') await expect(page.getByText('上一轮已经完成。')).toBeVisible();
    await page.getByLabel('发送 Agent 消息').fill('刚刚发送的消息');
    if (delivery !== 'direct') {
      await page.getByLabel('发送 Agent 消息').press('Enter');
      await expect(page.getByLabel('消息投递队列')).toContainText('刚刚发送的消息');
      expect(postCount).toBe(0);
      await expect.poll(() => Boolean(socket)).toBe(true);
      if (delivery === 'queued-guidance') {
        socket!.send(JSON.stringify({ type: 'event', event: { ...previousReply, payload: { ...previousReply.payload, final_reply: false } } }));
        await expect(page.locator('.conversation-activity-row.thought').getByText('上一轮已经完成。')).toBeVisible();
        await page.getByRole('button', { name: '调整方向排队消息 1' }).click();
        previousFinished = true;
      } else {
        previousFinished = true;
        socket!.send(JSON.stringify({ type: 'event', event: previousReply }));
      }
    } else await page.getByRole('button', { name: '发送消息' }).click();
    await expect.poll(() => submitted).toBe(true);
    const pending = page.locator('[data-user-event-id^="pending-user:"]');
    await expect(pending).toHaveCount(1);
    await pending.evaluate(element => { (element as HTMLElement).dataset.identityMarker = 'same-bubble'; });
    const turn = page.locator('.conversation-turn').filter({ has: page.locator('[data-identity-marker="same-bubble"]') });
    await turn.evaluate(element => { (element as HTMLElement).dataset.identityTurnMarker = 'same-turn'; });

    if (transport !== 'rest-after-ack') {
      showFormalUser = true;
      if (transport === 'stream-before-ack') {
        await expect.poll(() => Boolean(socket)).toBe(true);
        for (const event of current) socket!.send(JSON.stringify({ type: 'event', event }));
        await page.waitForTimeout(100);
      } else {
        const before = eventReads;
        await expect.poll(() => eventReads, { timeout: 10_000 }).toBeGreaterThan(before);
      }
      await expect(page.locator('.conversation-message.user')).toHaveCount(2);
      await expect(page.getByText('最初的消息', { exact: true })).toHaveCount(1);
      await expect(page.getByText('刚刚发送的消息', { exact: true })).toHaveCount(1);
      await expect(pending).toHaveCount(1);
      // The formal branch is known, but its cursor has not yet identified the
      // optimistic bubble. Its process must wait rather than attach to the
      // preceding user turn as an orphan.
      await expect(page.getByText('本轮正在读取包配置。', { exact: true })).toHaveCount(0);
    }
    releaseAcceptance?.();
    if (transport === 'rest-after-ack') showFormalUser = true;

    const formalUser = page.locator('[data-user-event-id="formal-user"]');
    await expect(formalUser).toHaveCount(1);
    await expect(formalUser).toHaveAttribute('data-identity-marker', 'same-bubble');
    await expect(pending).toHaveCount(0);
    await expect(page.locator('.conversation-message.user')).toHaveCount(2);
    await expect(page.getByText('最初的消息', { exact: true })).toHaveCount(1);
    await expect(page.getByText('刚刚发送的消息', { exact: true })).toHaveCount(1);
    const formalTurn = page.locator('[data-conversation-turn="formal-user"]');
    await expect(formalTurn).toHaveAttribute('data-identity-turn-marker', 'same-turn');
    await expect(formalTurn.getByText('本轮正在读取包配置。', { exact: true })).toBeVisible();
    await expect(page.locator('[data-conversation-turn="previous-user"] .conversation-message.assistant')).toContainText('上一轮已经完成。');

    finished = true;
    if (transport === 'stream-before-ack') socket!.send(JSON.stringify({ type: 'event', event: reply }));
    // REST-only variants deliberately omit both final MESSAGE and stream_closed.
    await expect(formalTurn.locator('[data-event-id="formal-reply"]')).toBeVisible({ timeout: 12_000 });
    await expect(page.locator('[data-event-id="formal-reply"]')).toHaveCount(1);
    await expect(page.getByRole('button', { name: '暂停当前 Agent' })).toHaveCount(0);
    const priorTurn = page.locator('[data-conversation-turn="previous-user"]');
    await expect(priorTurn.locator('.conversation-process-summary')).toHaveJSProperty('open', false);
    await expect(priorTurn.locator('.conversation-activity-row.thought').getByText('上一轮已经完成。')).toHaveCount(0);
    await expect(page.locator('.conversation-message.assistant')).toHaveCount(2);
    const beforeReload = await page.locator('.conversation-message.assistant').allTextContents();
    if (delivery === 'queued-guidance') await page.screenshot({ path: test.info().outputPath('queue-rendering.png') });
    expect(postCount).toBe(1);

    await page.reload();
    const restored = page.locator('[data-conversation-turn="formal-user"]');
    await expect(restored.locator('[data-event-id="formal-reply"]')).toBeVisible();
    await expect(page.locator('.conversation-message.user')).toHaveCount(2);
    await expect(page.getByText('最初的消息', { exact: true })).toHaveCount(1);
    await expect(page.getByText('刚刚发送的消息', { exact: true })).toHaveCount(1);
    await expect(page.locator('[data-event-id="formal-reply"]')).toHaveCount(1);
    await expect(page.locator('.conversation-message.assistant')).toHaveCount(2);
    expect(await page.locator('.conversation-message.assistant').allTextContents()).toEqual(beforeReload);
    expect(postCount).toBe(1);
  });
}

test('Historical reply projection uses the closest formal assistant ancestor without changing current progress', () => {
  const events: OpenHandsConversationEvent[] = [
    { id: 'current-progress', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'new-user', content: '继续检查', final_reply: false } },
    { id: 'new-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'state' } },
    { id: 'state', event_type: 'STATE', payload: { parent_id: 'prior-reply' } },
    { id: 'prior-reply', event_type: 'MESSAGE', payload: { source: 'assistant', parent_id: 'prior-progress', content: '检查完成', final_reply: false } },
    { id: 'prior-progress', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'old-user', content: '正在检查', final_reply: false } },
    { id: 'old-user', event_type: 'MESSAGE', payload: { source: 'human', parent_id: '__root__' } },
  ];
  const projected = projectHistoricalAgentReplies(events);
  expect(projected[3].payload.final_reply).toBe(true);
  expect(events[3].payload.final_reply).toBe(false);
  for (const index of [0, 1, 2, 4, 5]) expect(projected[index]).toBe(events[index]);
});

test('Historical reply projection never guesses across missing parents user boundaries or cycles', () => {
  const events: OpenHandsConversationEvent[] = [
    { id: 'unrelated-reply', event_type: 'MESSAGE', payload: { source: 'agent', content: '相同文本', final_reply: false } },
    { id: 'pending-user', event_type: 'MESSAGE', payload: { source: 'user', content: '相同文本' } },
    { id: 'missing-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'missing' } },
    { id: 'next-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'pending-user' } },
    { id: 'cycle-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'cycle-a' } },
    { id: 'cycle-a', event_type: 'STATE', payload: { parent_id: 'cycle-b' } },
    { id: 'cycle-b', event_type: 'STATE', payload: { parent_id: 'cycle-a' } },
  ];
  const projected = projectHistoricalAgentReplies(events);
  for (const [index, event] of events.entries()) expect(projected[index]).toBe(event);
});
