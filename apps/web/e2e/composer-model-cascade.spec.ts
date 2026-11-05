import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-11-05T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

const providers = [
  {
    id: 'provider-a', name: '供应商甲', connection_state: 'CONNECTED', is_primary: false,
    models: [
      { model_name: 'alpha-1', enabled: true, is_default: true },
      { model_name: 'alpha-2', enabled: true, is_default: false },
      { model_name: 'alpha-off', enabled: false, is_default: false },
    ],
  },
  {
    id: 'provider-b', name: '供应商乙', connection_state: 'CONNECTED', is_primary: true,
    models: [
      { model_name: 'beta-1', enabled: true, is_default: true, default_reasoning_effort: 'high', supported_reasoning_efforts: ['low', 'high'] },
      { model_name: 'beta-2', enabled: true, is_default: false },
    ],
  },
];

async function openConversation(page: Page, events: unknown[] = []) {
  let authenticated = false;
  const saved: Record<string, unknown>[] = [];
  const workspace = { id: 'cascade-workspace', display_name: '级联工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation: Record<string, unknown> = {
    id: 'cascade-conversation', display_title: '选择模型', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'finished', created_at: now, updated_at: now,
    model_provider_id: 'provider-a', model_name: 'alpha-1', reasoning_effort: null,
  };
  const batch = { events, next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } };
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const user = { id: 'cascade', username: 'cascade', role: 'USER', can_use_agent_sessions: true };
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'UNAUTHENTICATED', message: 'x' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, user); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [], waiting_user_input_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: batch, context: { model_name: 'alpha-1', window_tokens: 128_000, used_tokens: 100, usage_current: true }, readiness: { ready: true, execution_status: 'finished' } });
    if (path.endsWith('/events') && request.method() === 'GET') return json(route, batch);
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'finished' });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/pending-question')) return json(route, { pending: false });
    if (path.endsWith('/model') && request.method() !== 'GET') {
      const body = request.postDataJSON() as Record<string, unknown>;
      saved.push(body);
      Object.assign(conversation, body);
      return json(route, conversation);
    }
    if (path.endsWith('/context')) return json(route, { model_name: String(conversation.model_name), window_tokens: 128_000, used_tokens: 100, usage_current: true });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/model-providers')) return json(route, providers);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill('cascade');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto('/agent/conversations/cascade-conversation');
  return saved;
}

test('hovering a provider reveals its models and clicking a model applies it at once', async ({ page }) => {
  const saved = await openConversation(page);
  const summary = page.locator('.agent-composer-model-summary');
  await expect(summary).toContainText('alpha-1');
  await page.getByLabel(/打开模型与推理设置/).click();
  await expect(page.getByRole('button', { name: '确认' })).toHaveCount(0);

  // The current provider is shown first; disabled models are never offered.
  const models = page.getByRole('listbox', { name: '模型' });
  await expect(models.getByRole('option', { name: '模型 alpha-2' })).toBeVisible();
  await expect(models.getByRole('option', { name: '模型 alpha-off' })).toHaveCount(0);
  await expect(page.getByRole('option', { name: '供应商 供应商乙' })).toContainText('主要');
  // The selected model has no tinted background, only a check icon.
  const current = models.getByRole('option', { name: '模型 alpha-1' });
  await expect(current.locator('svg')).toHaveCount(1);
  expect(await current.evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgba(0, 0, 0, 0)');

  // Hover alone, with no click, swaps the model column to that provider.
  await page.getByRole('option', { name: '供应商 供应商乙' }).hover();
  await expect(models.getByRole('option', { name: '模型 beta-2' })).toBeVisible();
  await expect(models.getByRole('option', { name: '模型 alpha-2' })).toHaveCount(0);
  expect(saved).toHaveLength(0);

  await models.getByRole('option', { name: '模型 beta-2' }).click();
  await expect(page.locator('.agent-composer-model-popover')).toHaveCount(0);
  await expect.poll(() => saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({ model_provider_id: 'provider-b', model_name: 'beta-2' });
  await expect(summary).toContainText('beta-2');
});

test('Escape and an outside click close the menu without changing the model', async ({ page }) => {
  const saved = await openConversation(page);
  await page.getByLabel(/打开模型与推理设置/).click();
  await page.getByRole('option', { name: '供应商 供应商乙' }).hover();
  await page.keyboard.press('Escape');
  await expect(page.locator('.agent-composer-model-popover')).toHaveCount(0);
  await expect(page.locator('.agent-composer-model-summary')).toContainText('alpha-1');

  await page.getByLabel(/打开模型与推理设置/).click();
  await page.getByRole('option', { name: '供应商 供应商乙' }).hover();
  await page.mouse.click(8, 300);
  await expect(page.locator('.agent-composer-model-popover')).toHaveCount(0);
  expect(saved).toHaveLength(0);
});

test('keyboard focus on a provider reveals its models like hover', async ({ page }) => {
  await openConversation(page);
  await page.getByLabel(/打开模型与推理设置/).click();
  await page.getByRole('option', { name: '供应商 供应商乙' }).focus();
  await expect(page.getByRole('option', { name: '模型 beta-2' })).toBeVisible();
});

test('a user message that first used a new model shows a model-change divider above it', async ({ page }) => {
  const stamp = new Date().toISOString();
  const user = (id: string, content: string, extra: Record<string, unknown> = {}) => ({
    id, event_type: 'MESSAGE',
    payload: { source: 'user', content, display_content: content, timestamp: stamp, ...extra },
  });
  await openConversation(page, [
    user('u1', '第一条消息'),
    user('u2', '换模型后的消息', { model_change: { from: 'alpha-1 · 供应商甲', to: 'beta-1 · 供应商乙' } }),
  ]);
  const divider = page.locator('.conversation-model-change');
  await expect(divider).toHaveCount(1);
  await expect(divider).toContainText('模型已从 alpha-1 · 供应商甲 更改为 beta-1 · 供应商乙。');
  await expect(divider).toContainText('今天');
  // It sits directly above the message that carries it, not the first one.
  await expect(page.locator('.conversation-user-message', { hasText: '换模型后的消息' }).locator('.conversation-model-change')).toHaveCount(1);
  await expect(page.locator('.conversation-user-message', { hasText: '第一条消息' }).locator('.conversation-model-change')).toHaveCount(0);
});
