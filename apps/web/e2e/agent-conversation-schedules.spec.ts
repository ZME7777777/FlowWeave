import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-19T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function settleWorkspaceLayout(page: Page) {
  await page.evaluate(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    const elements = document.querySelectorAll('.agent-workspace-drawer, .agent-workspace-summary, .conversation-surface, .agent-composer-dock');
    await Promise.all([...elements].flatMap(element => element.getAnimations()).map(animation => animation.finished.catch(() => undefined)));
  });
}

async function openWorkspace(page: Page, events: unknown[] = [], includeSecondConversation = false) {
  const writes: { method: string; id?: string; body: Record<string, unknown> }[] = [];
  let authenticated = false;
  const workspace = { id: 'schedule-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'scheduled-conversation', display_title: '日报会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', created_at: now, updated_at: now,
  };
  const conversations = includeSecondConversation ? [conversation, { ...conversation, id: 'scheduled-conversation-2', display_title: '周报会话' }] : [conversation];
  const schedules = [{ id: 'daily-schedule', workspace_id: workspace.id, binding_id: null, work_directory_id: 'automation-directory', work_directory_name: '自动化 · 每日项目检查', workspace_mode: 'AUTOMATION', conversation_mode: 'NEW', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'medium', capability_version_ids: [], credential_ids: [], name: '每日项目检查', prompt: '请检查项目状态。', cron_expression: '0 9 * * 1-5', status: 'ACTIVE', next_run_at: '2026-10-20T01:00:00Z', last_run_at: now, row_version: 1, created_at: now, updated_at: now }];
  schedules.push({ ...schedules[0], id: 'weekly-schedule', name: '每周复核', prompt: '复核周报。', row_version: 7 });
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return authenticated
      ? json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true })
      : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, { id: 'schedule-user', username: 'schedule-user', role: 'USER', can_use_agent_sessions: true }); }
    if (path.endsWith('/agent-workspaces/default')) return json(route, workspace);
    if (path.endsWith('/conversation-management')) return json(route, { items: [], truncated: false });
    if (path.endsWith('/conversation-schedules') && request.method() === 'POST') {
      const body = request.postDataJSON();
      writes.push({ method: 'POST', body });
      const schedule = { ...schedules[0], ...body, id: 'created-schedule', row_version: 1 };
      schedules.push(schedule);
      return json(route, schedule);
    }
    if (path.includes('/conversation-schedules/') && request.method() === 'PUT') {
      const id = path.split('/').at(-1)!;
      const body = request.postDataJSON();
      writes.push({ method: 'PUT', id, body });
      const index = schedules.findIndex(item => item.id === id);
      schedules[index] = { ...schedules[index], ...body, row_version: schedules[index].row_version + 1 };
      return json(route, schedules[index]);
    }
    if (path.endsWith('/conversation-schedules')) return json(route, schedules);
    if (path.includes('/conversation-schedules/') && path.endsWith('/occurrences')) return json(route, Array.from({ length: 5 }, (_, index) => ({ id: `delivery-${index + 1}`, binding_id: index === 0 ? 'scheduled-run-1' : `scheduled-run-${index + 1}`, conversation_title: `每日项目检查 · 2026-10-${19 - index} 09:00`, scheduled_for: now, trigger_kind: 'SCHEDULED', state: 'STARTED', message_cursor: `event-${index + 1}`, created_at: now })));
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: { events, next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: { used_tokens: 136664, window_tokens: 512000, view_event_count: 68, condenser_max_size: 50 }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [{ id: 'existing-directory', display_name: '已有项目目录', current_version: { working_directory: '/runtime/workspace/project/existing-directory' } }] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'provider-1', name: '测试供应商', connection_state: 'CONNECTED', models: [{ model_name: 'model-1', enabled: true, is_default: true, default_reasoning_effort: 'medium', supported_reasoning_efforts: ['low', 'medium', 'high'] }] }]);
    if (path.endsWith('/website-credentials')) return json(route, [{ id: 'credential-1', name: '测试认证', target_host: 'example.test', target_path: '/', include_subdomains: false, auth_type: 'TOKEN', has_username: false, has_secret: true, row_version: 1, environment_names: { token: 'TEST_TOKEN' }, created_at: now, updated_at: now }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const bindingId = path.match(/\/conversations\/([^/]+)/)?.[1];
      return json(route, conversations.find(item => item.id === bindingId) ?? conversation);
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByLabel('账号').fill('schedule-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  return writes;
}

test('workspace directory search filters nested paths and restores the tree', async ({ page }) => {
  await openWorkspace(page);
  await page.route('**/api/v1/agent-workspaces/*/workspace?**', route => json(route, {
    root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
    working_directory: '/runtime/workspace/project', repositories: [], runtime: {},
    files: [
      { path: '/runtime/workspace/project/backend', kind: 'directory', size: 0 },
      { path: '/runtime/workspace/project/frontend', kind: 'directory', size: 0 },
      { path: '/runtime/workspace/project/frontend/src', kind: 'directory', size: 0 },
    ],
    ide: { gateway: { supported: false } },
  }));
  await page.getByRole('button', { name: '新增工作区', exact: true }).click();
  const creator = page.getByRole('dialog', { name: '新增工作区' });
  const search = creator.getByRole('searchbox', { name: '搜索目录' });
  await search.fill('frontend/src');
  await expect(creator.getByRole('checkbox', { name: 'frontend/src', exact: true })).toBeVisible();
  await expect(creator.getByRole('checkbox', { name: 'backend', exact: true })).toHaveCount(0);
  await search.fill('missing-directory');
  await expect(creator.getByText('没有匹配的目录。')).toBeVisible();
  await search.fill('');
  await expect(creator.getByRole('checkbox', { name: 'backend', exact: true })).toBeVisible();
  await creator.getByRole('button', { name: '关闭新增工作区' }).click();
});

test('selecting a task after opening its run returns to that task detail', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await tasks.locator('.agent-schedule-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toBeVisible();

  await tasks.locator('.agent-schedule-tree-select').filter({ hasText: '每周复核' }).click();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
  await expect(page.getByRole('heading', { name: '每周复核', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveCount(0);
});

test('scheduled session shows session configuration and expands additional runs', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await expect(tasks.locator('.agent-schedule-run')).toHaveCount(6);
  const firstSchedule = tasks.locator('.agent-schedule-tree-group').first();
  await expect(firstSchedule.getByRole('button', { name: '展开其余 2 个会话', exact: true })).toBeVisible();
  await firstSchedule.getByRole('button', { name: '展开其余 2 个会话', exact: true }).click();
  await expect(tasks.locator('.agent-schedule-run')).toHaveCount(8);
  await expect(firstSchedule.getByRole('button', { name: '收起会话', exact: true })).toBeVisible();

  await tasks.locator('.agent-schedule-run').first().click();
  await expect(page.getByRole('button', { name: '会话配置', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '会话配置', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '会话配置' })).toBeVisible();
});

test('internal navigation returns from schedules and management to the active conversation', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'active-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '保留当前会话', timestamp: now } },
    { id: 'active-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'active-user', content: '返回时应恢复这条回复。', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  await expect(page.getByText('返回时应恢复这条回复。', { exact: true })).toBeVisible();

  const navigation = page.getByRole('navigation', { name: 'Agent 工作区导航' });
  const drawer = page.locator('.agent-workspace-drawer');
  await expect(drawer).not.toHaveClass(/tools-open/);
  await navigation.getByRole('button', { name: '定时任务' }).click();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
  await expect(page.getByText('返回时应恢复这条回复。', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toBeVisible();
  await expect(drawer).not.toHaveClass(/tools-open/);

  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await expect(drawer).toHaveClass(/tools-open/);
  await navigation.getByRole('button', { name: '会话管理' }).click();
  await expect(page.getByRole('heading', { name: '会话管理', exact: true })).toBeVisible();
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
  await expect(page.getByText('返回时应恢复这条回复。', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toBeVisible();
  await expect(drawer).toHaveClass(/tools-open/);
});

test('internal navigation restores a draft in its originating work directory', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');

  const directoryGroup = page.locator('.agent-workspace-group').filter({ hasText: '已有项目目录' });
  await directoryGroup.getByRole('button', { name: '在已有项目目录中新建会话' }).click({ force: true });
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('保留已有项目目录中的草稿');
  const recoverDraft = directoryGroup.getByRole('button', { name: '恢复已有项目目录的未发送草稿' });
  await expect(recoverDraft).toHaveAttribute('aria-current', 'page');

  const navigation = page.getByRole('navigation', { name: 'Agent 工作区导航' });
  await navigation.getByRole('button', { name: '定时任务' }).click();
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(composer).toHaveValue('保留已有项目目录中的草稿');
  await expect(recoverDraft).toHaveAttribute('aria-current', 'page');

  await navigation.getByRole('button', { name: '会话管理' }).click();
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(composer).toHaveValue('保留已有项目目录中的草稿');
  await expect(recoverDraft).toHaveAttribute('aria-current', 'page');
});



test('scheduled conversations keep their route and draft while creating or editing a task', async ({ page }) => {
  const writes = await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
  await expect(page.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveCount(0);
  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await tasks.getByRole('button', { name: '新建', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新建定时任务' })).toBeVisible();
  await page.locator('.agent-schedule-create').getByRole('button', { name: '取消', exact: true }).click();
  await page.locator('.agent-schedule-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('保留当前会话草稿');

  await tasks.getByRole('button', { name: '新建', exact: true }).click();
  let editor = page.getByRole('dialog', { name: '新建定时任务', exact: true });
  await expect(editor).toBeVisible();
  await expect(editor.getByRole('textbox', { name: '任务名称', exact: true })).toBeFocused();
  await editor.getByRole('button', { name: /配置会话/ }).click();
  await expect(page.getByRole('dialog', { name: '会话配置' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '会话配置' })).toHaveCount(0);
  await expect(editor).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(editor).toHaveCount(0);
  await expect(composer).toHaveValue('保留当前会话草稿');

  await tasks.getByRole('button', { name: '新建', exact: true }).click();
  editor = page.getByRole('dialog', { name: '新建定时任务', exact: true });
  await editor.getByRole('textbox', { name: '任务名称', exact: true }).fill('新增巡检');
  await editor.getByLabel('业务提示词').fill('检查新增工作区');
  await editor.getByRole('button', { name: '保存任务', exact: true }).click();
  await expect(editor).toHaveCount(0);
  expect(writes[0]).toMatchObject({ method: 'POST', body: { name: '新增巡检', prompt: '检查新增工作区', workspace_mode: 'AUTOMATION' } });

  // Editing a different task must save its own ID/version, even after creating another task.
  await page.getByRole('button', { name: '每周复核 操作', exact: true }).click();
  await page.getByRole('menuitem', { name: '编辑', exact: true }).click();
  const edit = page.getByRole('dialog', { name: '编辑定时任务', exact: true });
  await expect(edit).toBeVisible();
  await expect(edit.getByRole('textbox', { name: '任务名称', exact: true })).toHaveValue('每周复核');
  await edit.getByLabel('业务提示词').fill('更新后的周报复核');
  await edit.getByRole('button', { name: '保存任务', exact: true }).click();
  await expect(edit).toHaveCount(0);
  expect(writes[1]).toMatchObject({ method: 'PUT', id: 'weekly-schedule', body: { prompt: '更新后的周报复核', expected_row_version: 7 } });
  await expect(composer).toHaveValue('保留当前会话草稿');
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await page.reload();
  await expect(tasks).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await page.goto('/agent/schedules');
  await expect(page.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
});

test('the floating card occupies conversation whitespace without resizing it or splitting its header', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const card = page.locator('.agent-workspace-summary');
  await expect(card).toContainText('IDEA / Gateway');
  const cardTransition = await card.evaluate(element => getComputedStyle(element).transitionProperty);
  expect(cardTransition).toContain('opacity');
  expect(cardTransition).toContain('transform');
  const geometry = () => page.evaluate(() => {
    const rect = (selector: string) => { const r = document.querySelector(selector)!.getBoundingClientRect(); return { left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height }; };
    const style=getComputedStyle(document.querySelector('.agent-workspace-summary article')!);
    return { main:rect('.agent-workbench-main'), header:rect('.agent-workbench-header'), content:rect('.agent-workbench-content'), composer:rect('.agent-composer'), drawer:rect('.agent-workspace-drawer'), card:rect('.agent-workspace-summary'), cardPresentation:{padding:style.padding,borderBottom:style.borderBottom} };
  });
  for (const width of [1280, 1920, 2560]) {
    await page.setViewportSize({ width, height: 1080 });
    await settleWorkspaceLayout(page);
    await expect.poll(async () => { const g=await geometry(); return Math.abs(g.header.right-g.main.right); }).toBeLessThanOrEqual(1);
    const before=await geometry();
    expect(before.card.left).toBeGreaterThanOrEqual(before.composer.right);
    expect(before.card.top).toBeGreaterThanOrEqual(before.header.bottom);
    expect(before.card.right).toBeLessThanOrEqual(before.main.right);
    expect(before.drawer.width).toBeLessThanOrEqual(1);
    expect(before.card.height).toBeLessThan(before.main.height-100);
    await expect(card.getByText('IDEA / Gateway')).toBeInViewport();
    await page.getByRole('button', { name:'隐藏悬浮信息卡', exact:true }).click();
    await expect(card).toBeHidden();
    await settleWorkspaceLayout(page);
    await expect.poll(async () => (await geometry()).composer.right).toBeGreaterThan(before.composer.right);
    const hidden=await geometry();
    expect(hidden.content.left).toBe(before.content.left);
    expect(hidden.content.right).toBe(before.content.right);
    await page.getByRole('button', { name:'显示悬浮信息卡', exact:true }).click();
    await expect(card).toBeVisible();
    await settleWorkspaceLayout(page);
  }
  const summaryPresentation=(await geometry()).cardPresentation;
  await page.getByRole('button', { name:'新建工作区页签', exact:true }).first().click();
  await expect(card).toBeHidden();
  await settleWorkspaceLayout(page);
  const before=await geometry();
  await page.getByRole('button', { name:'显示悬浮信息卡', exact:true }).click();
  await expect(card).toBeVisible();
  await settleWorkspaceLayout(page);
  await expect.poll(async () => (await geometry()).composer.right).toBeLessThan(before.composer.right);
  const after=await geometry();
  expect(after.cardPresentation).toEqual(summaryPresentation);
  expect(after.main).toEqual(before.main);
  expect(after.card.right).toBeLessThanOrEqual(after.main.right);
  expect(after.card.left).toBeGreaterThanOrEqual(after.composer.right);
  expect(after.card.height).toBeLessThan(after.main.height-100);
  await expect(card.getByText('IDEA / Gateway')).toBeInViewport();
});

test('closing a workspace tab does not reveal the hidden floating card', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');

  const card = page.locator('.agent-workspace-summary');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await expect(card).toBeHidden();
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await expect(card).toBeVisible();
  await page.getByRole('button', { name: '关闭新标签页页签', exact: true }).click();

  await expect(page.locator('.agent-workspace-drawer')).toHaveClass(/summary-open/);
  await expect(card).toBeHidden();
  await expect(page.getByRole('button', { name: '显示悬浮信息卡', exact: true })).toBeVisible();
});

test('the floating workspace card remains absent until workspace details load', async ({ page }) => {
  let releaseWorkspaceDetails: (() => void) | undefined;
  const workspaceDetailsReady = new Promise<void>(resolve => { releaseWorkspaceDetails = resolve; });

  await openWorkspace(page);
  await page.route('**/agent-workspaces/schedule-workspace/workspace', async route => {
    await workspaceDetailsReady;
    await json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
  });
  await page.goto('/agent/conversations/scheduled-conversation');

  await expect(page.locator('.agent-workspace-summary')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /悬浮信息卡/ })).toHaveCount(0);
  await expect(page.getByText('正在读取工作区…')).toHaveCount(0);

  releaseWorkspaceDetails?.();
  await expect(page.locator('.agent-workspace-summary')).toBeVisible();
  await expect(page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true })).toBeVisible();
});

test('hiding the floating card applies to every conversation and survives a reload', async ({ page }) => {
  await openWorkspace(page, [], true);
  await page.goto('/agent/conversations/scheduled-conversation');
  const card = page.locator('.agent-workspace-summary');

  await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
  await expect(card).toBeHidden();
  await page.getByRole('button', { name: '周报会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation-2$/);
  await expect(card).toBeHidden();
  await expect(page.getByRole('button', { name: '显示悬浮信息卡', exact: true })).toBeVisible();

  await page.reload();
  await expect(card).toBeHidden();
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await expect(card).toBeVisible();
});

test('summary toggles keep the card in place and reverse the conversation transition continuously', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1080 });
  await openWorkspace(page, [
    { id: 'motion-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看布局', timestamp: now } },
    { id: 'motion-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'motion-user', content: '显示和隐藏信息卡时，会话正文与输入草稿应保持原位并自然伸缩。'.repeat(12), final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('切换期间保留草稿');
  await expect(page.locator('.conversation-message.assistant')).toContainText('自然伸缩');
  await settleWorkspaceLayout(page);
  const card = page.locator('.agent-workspace-summary');
  const before = await card.boundingBox();
  const composerBefore = await page.locator('.agent-composer').boundingBox();
  await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
  const middle = await page.evaluate(() => {
    const card = document.querySelector('.agent-workspace-summary')!;
    const animations = [...document.querySelectorAll('.agent-workspace-summary, .agent-composer-dock, .conversation-surface')].flatMap(element => element.getAnimations());
    animations.forEach(animation => { animation.pause(); animation.currentTime = 90; });
    return {
      display: getComputedStyle(card).display, top: card.getBoundingClientRect().top, opacity: Number(getComputedStyle(card).opacity),
      padding: parseFloat(getComputedStyle(document.querySelector('.conversation-surface')!).paddingRight),
      margin: parseFloat(getComputedStyle(document.querySelector('.agent-composer-dock')!).marginRight),
      composerRight: document.querySelector('.agent-composer')!.getBoundingClientRect().right,
    };
  });
  expect(middle.display).not.toBe('none');
  expect(middle.top).toBe(before!.y);
  expect(middle.opacity).toBeGreaterThan(0);
  expect(middle.opacity).toBeLessThan(1);
  expect(middle.margin).toBeGreaterThan(0);
  expect(middle.padding - middle.margin).toBeCloseTo(16, 0);
  expect(middle.composerRight).toBeGreaterThan(composerBefore!.x + composerBefore!.width);
  await page.screenshot({ path: test.info().outputPath('summary-midpoint.png') });
  await page.evaluate(() => {
    document.querySelector('.conversation-message.assistant')!.setAttribute('data-motion-identity', 'preserved');
    document.getAnimations().forEach(animation => animation.play());
  });
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await settleWorkspaceLayout(page);
  await expect(card).toBeVisible();
  const after = await card.boundingBox();
  expect(after!.x).toBeCloseTo(before!.x, 0);
  expect(after!.y).toBe(before!.y);
  await expect(page.locator('[data-motion-identity="preserved"]')).toHaveCount(1);
  await expect(composer).toHaveValue('切换期间保留草稿');
  await page.screenshot({ path: test.info().outputPath('summary-restored.png') });

  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
  await expect(card).toBeHidden();
  expect(await page.locator('.agent-composer-dock').evaluate(element => element.getAnimations().length)).toBe(0);
  const composerHidden = await page.locator('.agent-composer').boundingBox();
  expect(middle.composerRight).toBeLessThan(composerHidden!.x + composerHidden!.width);
});

test('fullscreen tools occupy the entire workbench region and restore the conversation without extra grid rows', async ({page}) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer=page.getByRole('textbox',{name:'发送 Agent 消息'});
  await composer.fill('全屏切换时保留草稿');
  await page.getByRole('button',{name:'新建工作区页签',exact:true}).first().click();
  await page.getByRole('button',{name:'全屏查看工作区工具',exact:true}).click();
  const main=page.locator('.agent-workbench-main');
  await expect(main).toBeHidden();
  const controls=page.locator('.agent-workspace-tool-actions');
  await expect(controls.getByRole('button')).toHaveCount(3);
  await expect(controls.getByRole('button',{name:/^(显示|隐藏)悬浮信息卡$/})).toBeVisible();
  await expect(controls.getByRole('button',{name:'退出全屏',exact:true})).toBeVisible();
  await expect(controls.getByRole('button',{name:'关闭工作区工具',exact:true})).toBeVisible();
  await expect(page.locator('.agent-workspace-tab-launcher.active')).toBeVisible();
  const bounds=() => page.evaluate(() => {
    const root=document.querySelector('.agent-workbench-page')!.getBoundingClientRect();
    const rail=document.querySelector('.agent-workbench-rail')!.getBoundingClientRect();
    const drawer=document.querySelector('.agent-workspace-drawer')!.getBoundingClientRect();
    return Math.max(Math.abs(drawer.top-root.top),Math.abs(drawer.bottom-root.bottom),Math.abs(drawer.left-rail.right),Math.abs(drawer.right-root.right));
  });
  for (const width of [1024,1440,1920]) {
    await page.setViewportSize({width,height:1080});
    await expect.poll(bounds).toBeLessThanOrEqual(1);
  }
  const tabs=page.getByRole('navigation',{name:'工作区页签'});
  await tabs.getByRole('button',{name:'日报会话',exact:true}).click();
  await expect(main).toBeVisible();
  await expect(composer).toHaveValue('全屏切换时保留草稿');
  await expect.poll(() => page.evaluate(() => {
    const main=document.querySelector('.agent-workbench-main')!.getBoundingClientRect();
    const content=document.querySelector('.agent-workbench-content')!.getBoundingClientRect();
    return content.top-main.top;
  })).toBe(52);
  const contentWidth=await page.locator('.agent-workbench-content').evaluate(element=>element.getBoundingClientRect().width);
  await page.getByRole('button',{name:'显示悬浮信息卡',exact:true}).click();
  await expect(page.locator('.agent-workspace-summary')).toBeVisible();
  expect(await page.locator('.agent-workbench-content').evaluate(element=>element.getBoundingClientRect().width)).toBe(contentWidth);
  await expect(page.locator('.agent-workspace-summary').getByText('IDEA / Gateway')).toBeInViewport();
  await expect(page.locator('.agent-workspace-tab-launcher.active')).toBeHidden();
  await tabs.getByRole('button',{name:'新标签页',exact:true}).click();
  await expect(main).toBeHidden();
  await expect.poll(bounds).toBeLessThanOrEqual(1);
  await controls.getByRole('button',{name:'关闭工作区工具',exact:true}).click();
  await expect(main).toBeVisible();
  await expect(page.locator('.agent-workspace-summary')).toBeVisible();
  await expect(composer).toHaveValue('全屏切换时保留草稿');
});

test('HTML reports use a same-origin viewer URL that copied tabs reopen without Runtime proxying', async ({ page }) => {
  const reportPath = '/runtime/workspace/project/reports/daily.html';
  let browserProxyRequests = 0;
  await openWorkspace(page, [
    { id: 'report-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看日报', timestamp: now } },
    { id: 'report-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'report-user', content: '[打开 HTML 日报](reports/daily.html)', timestamp: now } },
  ]);
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/browser/**', async route => {
    browserProxyRequests += 1;
    await json(route, { error: { code: 'UNEXPECTED_BROWSER_PROXY', message: '同源查看链接不应由 Runtime 代理' } }, 500);
  });
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace/file**', async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('path') !== reportPath) return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
    await route.fulfill({
      status: 200,
      contentType: 'text/html',
      headers: { 'X-Preview-Total-Bytes': '105' },
      body: '<!doctype html><html><body><h1>会话日报</h1><script>document.body.dataset.scriptRan="true"</script></body></html>',
    });
  });

  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link', { name: '打开 HTML 日报' }).click();
  await expect(page.getByTitle('daily.html HTML 预览').contentFrame().getByRole('heading', { name: '会话日报' })).toBeVisible();
  await page.getByRole('dialog', { name: '文件预览' }).getByRole('button', { name: '在新标签页打开', exact: true }).click();

  const browserPanel = page.locator('.agent-workspace-browser-tab-panel.active');
  const address = browserPanel.getByRole('textbox', { name: '网页地址', exact: true });
  await expect(address).toHaveValue(/\/workspace-files\/view\?/);
  const viewerUrl = await address.inputValue();
  const viewerFrame = browserPanel.getByTitle('daily.html').contentFrame();
  await expect(viewerFrame.getByRole('heading', { name: 'daily.html' })).toBeVisible();
  await expect(viewerFrame.getByTitle('daily.html').contentFrame().getByRole('heading', { name: '会话日报' })).toBeVisible();
  await expect(viewerFrame.getByTitle('daily.html').contentFrame().locator('body')).not.toHaveAttribute('data-scriptRan', 'true');
  await expect(page.locator('.agent-workspace-tab-launcher')).toHaveCount(0);
  await expect(page.locator('.agent-workspace-browser-tab-panel')).toHaveCount(1);

  await page.getByRole('button', { name: '新建工作区页签', exact: true }).click();
  const launcher = page.locator('.agent-workspace-tab-launcher.active');
  const copiedAddress = launcher.getByRole('textbox', { name: '网页地址', exact: true });
  await copiedAddress.fill(viewerUrl);
  await copiedAddress.press('Enter');
  await expect(launcher.getByRole('textbox', { name: '网页地址', exact: true })).toHaveValue(viewerUrl);
  expect(browserProxyRequests).toBe(0);

  await page.goto(viewerUrl);
  await expect(page.getByRole('heading', { name: 'daily.html' })).toBeVisible();
  await expect(page.getByTitle('daily.html').contentFrame().getByRole('heading', { name: '会话日报' })).toBeVisible();
});



test('new workspace tabs embed a compact browser without a duplicate browser card', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();

  const launcher = page.locator('.agent-workspace-tab-launcher.active');
  await expect(launcher.getByRole('textbox', { name: '网页地址', exact: true })).toBeVisible();
  await expect(launcher.getByRole('button', { name: '浏览器', exact: true })).toHaveCount(0);
  await expect(launcher.getByRole('button', { name: '文件', exact: true })).toBeVisible();
  await expect.poll(() => launcher.locator('.agent-workspace-browser-toolbar').evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(48);
});


test('expanded tools squeeze and reflow the transcript and composer without covering them', async ({ page }) => {
  const prose = '这是一段用于验证侧栏挤压效果的完整回复，展开和拖动工具栏后应自动换行，发送框与按钮必须始终位于会话区域内。'.repeat(10);
  await openWorkspace(page, [
    { id: 'layout-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '检查侧栏展开后的弹性布局', timestamp: now } },
    { id: 'layout-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'layout-user', content: prose + '\n\n```text\n' + 'long_command_'.repeat(80) + '\n```', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('保留这段较长的输入草稿，展开侧栏后输入框应弹性收缩。'.repeat(4));
  await expect(page.locator('.conversation-message.assistant')).toContainText(prose);
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const geometry = () => page.evaluate(() => {
    const main = document.querySelector('.agent-workbench-main')!.getBoundingClientRect();
    const drawer = document.querySelector('.agent-workspace-drawer')!.getBoundingClientRect();
    const paragraph = document.querySelector('.conversation-message.assistant p')!.getBoundingClientRect();
    const composer = document.querySelector('.agent-composer')!.getBoundingClientRect();
    const elements = [...document.querySelectorAll('.agent-composer, .agent-composer textarea, .agent-composer footer, .agent-composer footer button, .agent-composer-bottom, .conversation-message, .conversation-message.assistant p, .conversation-message.assistant pre')];
    return {
      mainWidth: main.width, composerWidth: composer.width, paragraphHeight: paragraph.height,
      overlap: main.right - drawer.left,
      overflow: Math.max(...elements.map(element => { const box = element.getBoundingClientRect(); return Math.max(main.left - box.left, box.right - main.right); })),
      composerOverflow: document.querySelector('.agent-composer')!.scrollWidth - document.querySelector('.agent-composer')!.clientWidth,
    };
  });
  for (const width of [1920, 1440, 1280, 1024, 900]) {
    await page.setViewportSize({ width, height: 1080 });
    await expect.poll(async () => (await geometry()).overlap, `sidebar overlap at ${width}px`).toBeLessThanOrEqual(1);
    await expect.poll(async () => (await geometry()).overflow, `conversation overflow at ${width}px`).toBeLessThanOrEqual(1);
    await expect.poll(async () => (await geometry()).composerOverflow, `composer overflow at ${width}px`).toBeLessThanOrEqual(1);
  }
  await page.setViewportSize({ width: 1440, height: 1080 });
  await settleWorkspaceLayout(page);
  const before = await geometry();
  const resizer = await page.getByRole('separator', { name: '调整工作区工具宽度' }).boundingBox();
  await page.mouse.move(resizer!.x + resizer!.width - 1, resizer!.y + 100);
  await page.mouse.down();
  await page.mouse.move(resizer!.x - 130, resizer!.y + 100, { steps: 12 });
  await page.mouse.up();
  await expect.poll(async () => (await geometry()).mainWidth).toBeLessThan(before.mainWidth - 100);
  await settleWorkspaceLayout(page);
  const after = await geometry();
  expect(after.composerWidth).toBeLessThan(before.composerWidth - 100);
  expect(after.paragraphHeight).toBeGreaterThan(before.paragraphHeight);
  expect(after.overflow).toBeLessThanOrEqual(1);
  expect(after.composerOverflow).toBeLessThanOrEqual(1);
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await expect(page.locator('.agent-workspace-summary')).toBeVisible();
  await expect.poll(async () => (await geometry()).overflow).toBeLessThanOrEqual(1);
  await expect.poll(async () => (await geometry()).composerOverflow).toBeLessThanOrEqual(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => {
    const main = document.querySelector('.agent-workbench-main')!.getBoundingClientRect();
    const drawer = document.querySelector('.agent-workspace-drawer')!.getBoundingClientRect();
    return main.bottom - drawer.top;
  })).toBeLessThanOrEqual(1);
  await expect.poll(async () => (await geometry()).overflow).toBeLessThanOrEqual(1);
  await expect(composer).toHaveValue('保留这段较长的输入草稿，展开侧栏后输入框应弹性收缩。'.repeat(4));
});


async function splitGeometry(page: Page) {
  return page.evaluate(() => {
    const drawer = document.querySelector('.agent-workspace-drawer')!.getBoundingClientRect();
    const rail = document.querySelector('.agent-workbench-rail')!.getBoundingClientRect();
    const parent = document.querySelector('.agent-workspace-drawer')!.parentElement!.getBoundingClientRect();
    return { width: drawer.width, available: parent.right - rail.right };
  });
}

async function dragToolTo(page: Page, width: number) {
  await settleWorkspaceLayout(page);
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const box = await resizer.boundingBox();
  const current = await splitGeometry(page);
  const x = box!.x + box!.width / 2;
  const y = box!.y + 240;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + current.width - width, y, { steps: 12 });
  await page.mouse.up();
  await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
}

test('tool boundaries follow available space and small boundary movements keep the split', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const drawer = page.locator('.agent-workspace-drawer');
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const maxima: number[] = [], minima: number[] = [];
  for (const viewport of [1024, 1440, 1920, 2560]) {
    await page.setViewportSize({ width: viewport, height: 1080 });
    await settleWorkspaceLayout(page);
    const { available } = await splitGeometry(page);
    await dragToolTo(page, available * .75);
    await expect.poll(async () => Math.abs((await splitGeometry(page)).width - available * .75)).toBeLessThan(1);
    maxima.push((await splitGeometry(page)).width);
    await resizer.click();
    await dragToolTo(page, available * .75 + 12);
    await expect(drawer).not.toHaveClass(/fullscreen/);
    await dragToolTo(page, available * .25);
    await expect.poll(async () => Math.abs((await splitGeometry(page)).width - available * .25)).toBeLessThan(1);
    minima.push((await splitGeometry(page)).width);
    await resizer.click();
    await dragToolTo(page, available * .25 - 12);
    await expect(drawer).toHaveClass(/tools-open/);
  }
  expect(maxima[3]).toBeGreaterThan(maxima[0] * 2);
  expect(minima[3]).toBeGreaterThan(minima[0] * 2);
  const beforeRail = await splitGeometry(page);
  const railHandle = page.getByRole('separator', { name: '调整会话列表宽度' });
  const railBox = await railHandle.boundingBox();
  await page.mouse.move(railBox!.x + 1, railBox!.y + 180);
  await page.mouse.down();
  await page.mouse.move(railBox!.x + 140, railBox!.y + 180, { steps: 12 });
  await page.mouse.up();
  await expect.poll(async () => (await splitGeometry(page)).available).toBeLessThan(beforeRail.available - 100);
  const afterRail = await splitGeometry(page);
  await dragToolTo(page, afterRail.available * .75);
  await expect.poll(async () => Math.abs((await splitGeometry(page)).width - afterRail.available * .75)).toBeLessThan(1);
});

test('hidden preview remains hidden on same-direction release and capture or focus ending', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1080 });
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('松手后保持隐藏');
  const shell = page.locator('.agent-workspace-tool-shell');
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  for (const ending of ['pointerup', 'blur', 'lostpointercapture']) {
    await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
    await settleWorkspaceLayout(page);
    const { available, width: initialWidth } = await splitGeometry(page);
    const delta = initialWidth - available * .18;
    const box = await resizer.boundingBox();
    const x = box!.x + box!.width / 2, y = box!.y + 240;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + delta, y, { steps: 12 });
    await expect(shell).toBeHidden();
    await page.mouse.move(x + delta + available * .02, y, { steps: 8 });
    await page.mouse.move(x + delta + available * .02 - 2, y);
    await expect(shell).toBeHidden();
    if (ending === 'blur') await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    if (ending === 'lostpointercapture') await resizer.evaluate(element => element.dispatchEvent(new PointerEvent('lostpointercapture', { pointerId: 1 })));
    await page.mouse.up();
    await expect(shell).toBeHidden();
    await expect(page.locator('.agent-workspace-summary')).toBeHidden();
    await expect(resizer).toHaveCount(0);
    await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
    await expect(composer).toHaveValue('松手后保持隐藏');
  }
});

test('held tool drags require deliberate reversal and keep iframe capture across both modes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1080 });
  await openWorkspace(page);
  await page.route('**/browser/**', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<html><body style="margin:0;height:100vh">可拖过的网页</body></html>' }));
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('两端拖回继续编辑');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const address = page.getByRole('textbox', { name: '网页地址', exact: true });
  await address.fill('https://resize-frame.test/');
  await address.press('Enter');
  await expect(page.locator('.agent-workspace-browser-frame').contentFrame().getByText('可拖过的网页')).toBeVisible();
  const drawer = page.locator('.agent-workspace-drawer');
  const shell = page.locator('.agent-workspace-tool-shell');
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const { available } = await splitGeometry(page);
  for (const mode of ['fullscreen', 'hidden']) {
    await dragToolTo(page, available * (mode === 'fullscreen' ? .75 : .25));
    const box = await resizer.boundingBox();
    const x = box!.x + box!.width / 2, y = box!.y + 240;
    const direction = mode === 'fullscreen' ? -1 : 1;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + direction * available * .07, y, { steps: 12 });
    if (mode === 'fullscreen') await expect(drawer).toHaveClass(/fullscreen/);
    else await expect(shell).toBeHidden();
    await page.mouse.move(x + direction * available * .06, y, { steps: 6 });
    if (mode === 'fullscreen') await expect(drawer).toHaveClass(/fullscreen/);
    else await expect(shell).toBeHidden();
    await page.mouse.move(x - direction * available * .05, y, { steps: 12 });
    await expect(drawer).not.toHaveClass(/fullscreen/);
    await expect(shell).toBeVisible();
    await expect(page.locator('body')).toHaveClass(/agent-workspace-resizing/);
    await page.mouse.up();
    await expect(drawer).toHaveClass(/tools-open/);
    await expect(composer).toHaveValue('两端拖回继续编辑');
  }
  await dragToolTo(page, available * .85);
  await expect(drawer).toHaveClass(/fullscreen/);
  await page.getByRole('button', { name: '退出全屏', exact: true }).click();
  await expect(resizer).toBeVisible();
  await resizer.click();
  await expect(drawer).not.toHaveClass(/fullscreen/);
});

test('message scale hides when the conversation gutter collapses and returns after resizing tools', async ({page}) => {
  await page.setViewportSize({width:1920,height:1080});
  await openWorkspace(page,[
    {id:'scale-user',event_type:'MESSAGE',payload:{source:'user',parent_id:'__root__',content:'检查刻度条是否遮挡正文',timestamp:now}},
    {id:'scale-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'scale-user',content:'会话区收窄后，刻度条应该自动隐藏。'.repeat(15),final_reply:true,timestamp:now}},
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const scale=page.getByRole('navigation',{name:'用户消息导航'});
  await expect(scale).toBeVisible();
  await page.getByRole('button',{name:'新建工作区页签',exact:true}).first().click();
  await expect(scale).toBeVisible();
  const resizeTo = (targetWidth: number) => dragToolTo(page, targetWidth);
  const { available } = await splitGeometry(page);
  // Keep these layout assertions below the threshold that intentionally opens fullscreen.
  await resizeTo(available * .43);
  await expect(scale).toBeHidden();
  await expect(page.locator('.conversation-message-index-tooltip')).toBeHidden();
  await expect(page.locator('.conversation-message.assistant')).toContainText('会话区收窄后');
  await resizeTo(available * .25);
  await expect(scale).toBeVisible();
  await page.setViewportSize({ width: 2560, height: 1080 });
  await settleWorkspaceLayout(page);
  const wide = (await splitGeometry(page)).available;
  await resizeTo(wide * .25);
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await expect(scale).toBeVisible();
  await resizeTo(wide * .5);
  await expect(scale).toBeHidden();
  await resizeTo(wide * .25);
  await expect(scale).toBeVisible();
  const gap=await page.evaluate(() => document.querySelector('.conversation-message.assistant p')!.getBoundingClientRect().left-document.querySelector('.conversation-message-index')!.getBoundingClientRect().right);
  expect(gap).toBeGreaterThanOrEqual(8);
});
