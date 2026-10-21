import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-19T00:00:00Z';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function settleWorkspaceLayout(page: Page) {
  await page.evaluate(async () => {
    const elements = document.querySelectorAll('.agent-workspace-drawer, .agent-workspace-summary, .conversation-surface, .agent-composer-dock');
    await Promise.all([...elements].flatMap(element => element.getAnimations()).map(animation => animation.finished.catch(() => undefined)));
  });
}

async function openWorkspace(page: Page, events: unknown[] = []) {
  const writes: { method: string; id?: string; body: Record<string, unknown> }[] = [];
  let authenticated = false;
  const workspace = { id: 'schedule-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'scheduled-conversation', display_title: '日报会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', created_at: now, updated_at: now,
  };
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
    if (path.includes('/conversation-schedules/') && path.endsWith('/occurrences')) return json(route, [{ id: 'delivery-1', binding_id: 'scheduled-run-1', conversation_title: '每日项目检查 · 2026-10-19 09:00', scheduled_for: now, trigger_kind: 'SCHEDULED', state: 'STARTED', message_cursor: 'event-1', created_at: now }]);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: [conversation], next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: { events, next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: { used_tokens: 136664, window_tokens: 512000, view_event_count: 68, condenser_max_size: 50 }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [{ id: 'existing-directory', display_name: '已有项目目录', current_version: { working_directory: '/runtime/workspace/project/existing-directory' } }] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'provider-1', name: '测试供应商', connection_state: 'CONNECTED', models: [{ model_name: 'model-1', enabled: true, is_default: true, default_reasoning_effort: 'medium', supported_reasoning_efforts: ['low', 'medium', 'high'] }] }]);
    if (path.endsWith('/website-credentials')) return json(route, [{ id: 'credential-1', name: '测试认证', target_host: 'example.test', target_path: '/', include_subdomains: false, auth_type: 'TOKEN', has_username: false, has_secret: true, row_version: 1, environment_names: { token: 'TEST_TOKEN' }, created_at: now, updated_at: now }]);
    if (path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') return json(route, conversation);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByLabel('账号').fill('schedule-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  return writes;
}

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

  const navigation = page.getByRole('navigation', { name: 'Agent 工作区导航' });
  await navigation.getByRole('button', { name: '定时任务' }).click();
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(composer).toHaveValue('保留已有项目目录中的草稿');
  await expect(directoryGroup.getByRole('button', { name: '恢复已有项目目录的未发送草稿' })).toHaveCount(0);

  await navigation.getByRole('button', { name: '会话管理' }).click();
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(composer).toHaveValue('保留已有项目目录中的草稿');
  await expect(directoryGroup.getByRole('button', { name: '恢复已有项目目录的未发送草稿' })).toHaveCount(0);
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
    const hidden=await geometry();
    expect(hidden.composer.right).toBeGreaterThan(before.composer.right);
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
  const after=await geometry();
  expect(after.cardPresentation).toEqual(summaryPresentation);
  expect(after.main).toEqual(before.main);
  expect(after.composer.right).toBeLessThan(before.composer.right);
  expect(after.card.right).toBeLessThanOrEqual(after.main.right);
  expect(after.card.left).toBeGreaterThanOrEqual(after.composer.right);
  expect(after.card.height).toBeLessThan(after.main.height-100);
  await expect(card.getByText('IDEA / Gateway')).toBeInViewport();
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


test('tool boundaries remain draggable after automatic fullscreen and across an embedded browser', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.addInitScript(() => {
    if (!localStorage.getItem('flowweave:workspace-tool-width')) localStorage.setItem('flowweave:workspace-tool-width', '720');
  });
  await openWorkspace(page);
  await page.route('**/browser/**', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<html><body style="margin:0;height:100vh;background:#eef5ef">可拖过的浏览器页面</body></html>' }));
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('调整工具宽度时保留草稿');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const drawer = page.locator('.agent-workspace-drawer');
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const width = () => drawer.evaluate(element => element.getBoundingClientRect().width);
  await expect(resizer).toBeVisible();
  await expect.poll(width).toBe(720);
  const address = page.getByRole('textbox', { name: '网页地址', exact: true });
  await address.fill('https://resize-frame.test/');
  await address.press('Enter');
  await expect(page.locator('.agent-workspace-browser-frame').contentFrame().getByText('可拖过的浏览器页面')).toBeVisible();
  const drag = async (delta: number) => {
    const box = await resizer.boundingBox();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + 240);
    await page.mouse.down();
    await expect(page.locator('body')).toHaveClass(/agent-workspace-resizing/);
    await page.mouse.move(box!.x + box!.width / 2 + delta, box!.y + 240, { steps: 12 });
    await page.mouse.up();
    await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
  };
  await drag(160);
  await expect.poll(width).toBe(560);
  await drag(-260);
  await expect(drawer).toHaveClass(/fullscreen/);
  await page.getByRole('button', { name: '退出全屏', exact: true }).click();
  await expect.poll(width).toBe(720);
  await expect(drawer).not.toHaveClass(/fullscreen/);
  await expect(resizer).toBeVisible();
  await drag(420);
  await expect.poll(width).toBe(300);
  await drag(-100);
  await expect.poll(width).toBe(400);
  await expect(composer).toHaveValue('调整工具宽度时保留草稿');
  const box = await resizer.boundingBox();
  await page.mouse.move(box!.x + 4, box!.y + 240);
  await page.mouse.down();
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
  await page.mouse.up();
  expect(await page.locator('body').evaluate(element => element.style.cursor)).toBe('');
  await page.mouse.move(box!.x + 4, box!.y + 240);
  await page.mouse.down();
  await expect(page.locator('body')).toHaveClass(/agent-workspace-resizing/);
  await page.getByRole('button', { name: '关闭工作区工具', exact: true }).evaluate(button => button.click());
  await expect(resizer).toHaveCount(0);
  await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
  await page.mouse.up();
  expect(await page.locator('body').evaluate(element => element.style.userSelect)).toBe('');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.setViewportSize({ width: 1024, height: 1080 });
  await expect.poll(width).toBe(324);
  await expect(resizer).toBeVisible();
  await drag(20);
  await expect.poll(width).toBe(304);
  await expect(composer).toHaveValue('调整工具宽度时保留草稿');
});

test('held boundary drags preview both extremes reversibly while clicks keep the split unchanged', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.addInitScript(() => localStorage.setItem('flowweave:workspace-tool-width', '720'));
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('两端拖回后继续编辑');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const drawer = page.locator('.agent-workspace-drawer');
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const shell = page.locator('.agent-workspace-tool-shell');
  const main = page.locator('.agent-workbench-main');
  const width = () => drawer.evaluate(element => element.getBoundingClientRect().width);
  await resizer.click();
  await expect(drawer).not.toHaveClass(/fullscreen/);
  await expect.poll(width).toBe(720);
  const box = await resizer.boundingBox();
  const startX = box!.x + box!.width / 2;
  await page.mouse.move(startX, box!.y + 240);
  await page.mouse.down();
  await page.mouse.move(startX - 50, box!.y + 240, { steps: 8 });
  await expect(drawer).toHaveClass(/fullscreen/);
  await expect(main).toBeHidden();
  await expect(page.locator('body')).toHaveClass(/agent-workspace-resizing/);
  await page.mouse.move(startX + 160, box!.y + 240, { steps: 12 });
  await expect(drawer).not.toHaveClass(/fullscreen/);
  await expect(main).toBeVisible();
  await expect.poll(width).toBe(560);
  await page.mouse.move(startX + 470, box!.y + 240, { steps: 12 });
  await expect(drawer).toHaveClass(/summary-open/);
  await expect(shell).toBeHidden();
  await expect(page.locator('body')).toHaveClass(/agent-workspace-resizing/);
  await page.mouse.move(startX + 300, box!.y + 240, { steps: 12 });
  await expect(drawer).toHaveClass(/tools-open/);
  await expect(shell).toBeVisible();
  await expect.poll(width).toBe(420);
  await page.mouse.up();
  await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
  await expect(composer).toHaveValue('两端拖回后继续编辑');
  const minimumBox = await resizer.boundingBox();
  await page.mouse.move(minimumBox!.x + 5, minimumBox!.y + 240);
  await page.mouse.down();
  await page.mouse.move(minimumBox!.x + 125, minimumBox!.y + 240, { steps: 8 });
  await page.mouse.up();
  await expect.poll(width).toBe(300);
  await resizer.click();
  await expect(drawer).toHaveClass(/tools-open/);
  await expect.poll(width).toBe(300);
  const hideBox = await resizer.boundingBox();
  await page.mouse.move(hideBox!.x + 5, hideBox!.y + 240);
  await page.mouse.down();
  await page.mouse.move(hideBox!.x + 55, hideBox!.y + 240, { steps: 8 });
  await expect(shell).toBeHidden();
  await page.mouse.up();
  await expect(resizer).toHaveCount(0);
  await expect(main).toBeVisible();
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await expect(shell).toBeVisible();
  await expect(composer).toHaveValue('两端拖回后继续编辑');
  const cancelBox = await resizer.boundingBox();
  await page.mouse.move(cancelBox!.x + 5, cancelBox!.y + 240);
  await page.mouse.down();
  await page.mouse.move(cancelBox!.x + 55, cancelBox!.y + 240, { steps: 8 });
  await expect(shell).toBeHidden();
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect(shell).toBeVisible();
  await expect(drawer).toHaveClass(/tools-open/);
  await expect(page.locator('body')).not.toHaveClass(/agent-workspace-resizing/);
  await page.mouse.up();
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
  const resizeTo=async (targetWidth:number) => {
    const box=await page.getByRole('separator',{name:'调整工作区工具宽度'}).boundingBox();
    const currentWidth=await page.locator('.agent-workspace-drawer').evaluate(element=>element.getBoundingClientRect().width);
    const startX=box!.x+box!.width/2;
    await page.mouse.move(startX,box!.y+100);
    await page.mouse.down();
    await page.mouse.move(startX+currentWidth-targetWidth,box!.y+100,{steps:12});
    await page.mouse.up();
  };
  // Keep these layout assertions below the threshold that intentionally opens fullscreen.
  await resizeTo(700);
  await expect(scale).toBeHidden();
  await expect(page.locator('.conversation-message-index-tooltip')).toBeHidden();
  await expect(page.locator('.conversation-message.assistant')).toContainText('会话区收窄后');
  await resizeTo(310);
  await expect(scale).toBeVisible();
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
  await expect(scale).toBeVisible();
  await resizeTo(470);
  await expect(scale).toBeHidden();
  await resizeTo(310);
  await expect(scale).toBeVisible();
  const gap=await page.evaluate(() => document.querySelector('.conversation-message.assistant p')!.getBoundingClientRect().left-document.querySelector('.conversation-message-index')!.getBoundingClientRect().right);
  expect(gap).toBeGreaterThanOrEqual(8);
});
