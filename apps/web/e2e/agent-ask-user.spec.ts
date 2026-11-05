import { expect, test, type Route } from '@playwright/test';

const now = '2026-11-05T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function openAskedConversation(page: import('@playwright/test').Page, followUps: unknown[] = [], finalReply = true) {
  let authenticated = false;
  const sent: string[] & { asked?: unknown[] } = [];
  const workspace = { id: 'ask-user-workspace', display_name: '提问工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'ask-user-conversation', display_title: '等待回答的会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'finished', created_at: now, updated_at: now,
  };
  const events: Array<Record<string, unknown>> = [
    { id: 'ask-user-first', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '部署到生产吗', timestamp: now } },
    { id: 'ask-user-action', event_type: 'TOOL_CALL', payload: { tool_name: 'ask_user', event_name: 'AskUserAction', action_id: 'ask-user-action', tool_call_id: 'call-1', parent_id: 'ask-user-first', timestamp: now, details: { question: '是否把版本 2.3 发布到生产环境？', options: ['发布', '先在灰度验证'], recommendation: '建议先灰度，回滚更便宜' } } },
    { id: 'ask-user-result', event_type: 'TOOL_RESULT', payload: { tool_name: 'ask_user', event_name: 'AskUserObservation', action_id: 'ask-user-action', tool_call_id: 'call-1', parent_id: 'ask-user-action', timestamp: now, details: { asked: true } } },
  ];
  if (finalReply) events.push({ id: 'ask-user-final', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'ask-user-result', content: '请确认是否发布。', timestamp: now } });
  const asked: unknown[] = [];
  sent.asked = asked;
  const batch = { events, next_cursor: events.at(-1)!.id, history_cursor: null, result: { status: 'COMPLETED' } };
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated ? json(route, { id: 'ask-user', username: 'ask-user', role: 'USER', can_use_agent_sessions: true }) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, { id: 'ask-user', username: 'ask-user', role: 'USER', can_use_agent_sessions: true }); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [], waiting_user_input_binding_ids: sent.length ? [] : [conversation.id], waiting_user_input_actions: sent.length ? {} : { [conversation.id]: 'ask-user-action' } });
    if (path.endsWith('/hydration')) return json(route, { events: batch, context: { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true }, readiness: { ready: true, execution_status: 'finished' } });
    if (path.endsWith('/events') && request.method() === 'GET') return json(route, batch);
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'finished' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/pending-question')) return json(route, { pending: true, action_id: 'ask-user-action', tool_call_id: 'call-1', question: '是否把版本 2.3 发布到生产环境？', options: ['发布', '先在灰度验证'], recommendation: '建议先灰度，回滚更便宜', questions: followUps });
    if (path.endsWith('/messages') && request.method() === 'POST') { const body = request.postDataJSON(); sent.push(body.content); asked.push(body.answered_question); return json(route, { accepted: true, cursor: 'ask-user-reply' }, 202); }
    if (path.endsWith('/context')) return json(route, { model_name: 'test-model', window_tokens: 128_000, used_tokens: 100, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill('ask-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto('/agent/conversations/ask-user-conversation');
  return sent;
}

test('an open ask_user question shows the sidebar badge, the question card and a history row', async ({ page }) => {
  await openAskedConversation(page, [], false);
  const card = page.getByTestId('agent-user-question');
  await expect(card).toBeVisible();
  await expect(card.getByText('是否把版本 2.3 发布到生产环境？')).toBeVisible();
  await expect(page.locator('[data-conversation-binding-id="ask-user-conversation"]').getByText('需要用户输入')).toBeVisible();
  await expect(page.locator('[data-ask-user-event-id="ask-user-action"]')).toContainText('是否把版本 2.3 发布到生产环境？');
});

test('Escape closes the card and a typed reply is sent as an ordinary user message', async ({ page }) => {
  const sent = await openAskedConversation(page);
  const card = page.getByTestId('agent-user-question');
  await expect(card).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(card).toHaveCount(0);
  // The composer stays usable for a free-form reply after the card is dismissed.
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  // A closed question stays closed after a refresh, and its sidebar badge goes away.
  await page.reload();
  await expect(page.getByPlaceholder('给 Agent 发消息…')).toBeVisible();
  await expect(page.getByTestId('agent-user-question')).toHaveCount(0);
  await expect(page.locator('[data-conversation-binding-id="ask-user-conversation"]').getByText('需要用户输入')).toHaveCount(0);
  await page.getByPlaceholder('给 Agent 发消息…').fill('先灰度');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect.poll(() => [...sent]).toEqual(['先灰度']);
});

test('skipping after the final reply sends nothing and just closes the card', async ({ page }) => {
  const sent = await openAskedConversation(page);
  await expect(page.getByPlaceholder('给 Agent 发消息…')).toBeVisible();
  await page.waitForTimeout(1500);
  await page.getByTestId('agent-user-question').getByRole('button', { name: '跳过' }).click();
  await expect(page.getByTestId('agent-user-question')).toHaveCount(0);
  await page.waitForTimeout(500);
  expect([...sent]).toEqual([]);
});

test('the question card sits above the composer, not inside it', async ({ page }) => {
  await openAskedConversation(page);
  const card = await page.getByTestId('agent-user-question').boundingBox();
  const composer = await page.locator('.agent-composer').boundingBox();
  expect(card && composer && card.y + card.height <= composer.y + 1).toBe(true);
});

test('several questions are answered step by step and submitted once with the questions attached', async ({ page }) => {
  const sent = await openAskedConversation(page, [{ question: '回滚策略？', options: ['自动回滚', '保留现场'] }]);
  const card = page.getByTestId('agent-user-question');
  await expect(card.getByText('1 / 2')).toBeVisible();
  await card.getByRole('button', { name: /先在灰度验证/ }).click();
  // Choosing an option moves on to the next question by itself.
  await expect(card.getByText('回滚策略？')).toBeVisible();
  await expect(card.getByText('2 / 2')).toBeVisible();
  await card.getByRole('button', { name: '上一题' }).click();
  await expect(card.getByText('是否把版本 2.3 发布到生产环境？')).toBeVisible();
  await card.getByRole('button', { name: '下一题' }).click();
  await card.getByRole('button', { name: '跳过' }).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toBe('1. 先在灰度验证\n2. （已跳过，请自行判断并说明假设）');
  expect(sent.asked?.[0]).toBe('1. 是否把版本 2.3 发布到生产环境？\n2. 回滚策略？');
});
