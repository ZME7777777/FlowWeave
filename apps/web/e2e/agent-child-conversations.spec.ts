import { expect, test, type Page, type Route, type WebSocketRoute } from '@playwright/test';

const now = '2026-11-05T00:00:00Z';
const parentId = 'child-product-parent';
const otherParentId = 'child-product-other';
const children = Array.from({ length: 8 }, (_, index) => ({
  id: `native-child-${index + 1}`, parent_binding_id: parentId,
  display_title: index === 2 ? '很长的子会话标题用来检查标签和悬浮卡不会因为内容被撑开'.repeat(3) : `独立任务 ${index + 1}`,
  lifecycle: 'ACTIVE', unread: index !== 0, unread_origin: index === 1 ? 'SYSTEM' : 'MANUAL', created_at: now,
}));
async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}
async function openParent(page: Page, host: 'workspace' | 'node', delayFirst = false) {
  let authenticated = false;
  const writes: string[] = [];
  const requests: string[] = [];
  let release: (() => void) | undefined;
  let peak = 0;
  let reads = 0;
  let shouldDelay = delayFirst;
  const statuses = new Map<string, string>();
  const nativeEvents = new Map<string, Array<{ id: string; event_type: string; payload: Record<string, unknown> }>>();
  const messages: Array<{ childId: string; payload: Record<string, unknown> }> = [];
  const childWrites: Array<{ childId: string; action: string; payload: Record<string, unknown> }> = [];
  const models = new Map<string, string>();
  const streams = new Map<string, WebSocketRoute>();
  const held: { nextSend: boolean; release?: () => void; failSend: boolean; hideAccepted: boolean } = { nextSend: false, failSend: false, hideAccepted: false };
  const providers = [{ id: 'child-provider', name: '测试模型', connection_state: 'CONNECTED', provider_type: 'OPENAI', models: ['child-model', 'child-model-two'].map((model_name, index) => ({ model_name, enabled: true, is_default: index === 0, supported_reasoning_efforts: ['low', 'high'], default_reasoning_effort: 'low' })) }];
  const unread = new Set(children.filter(child => child.unread).map(child => child.id));
  const root = host === 'node' ? '/flow-runs/child-run/nodes/child-node/attempts/child-attempt/agent-sessions' : '/agent/conversations';
  const workspace = { id: host === 'node' ? 'child-run' : 'children-workspace', display_name: '子会话工作区', desired_state: 'RUNNING', updated_at: now };
  const parents = [parentId, otherParentId].map((id, index) => ({ id, display_title: index ? '另一个父会话' : '父会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'finished', created_at: now, updated_at: now, child_summary: id === parentId ? { total: 8, unread: unread.size, running: 1, attention: 1, condensing: 0, unknown: 0, pending: 0 } : undefined }));
  const activity = () => children.map((child, index) => ({ id: child.id, parent_binding_id: parentId, execution_status: index === 0 ? 'running' : index === 1 ? 'error' : 'finished', possibly_stuck: false, failed: index === 1, condensing: false, sync_pending: false, unread: unread.has(child.id), unread_origin: child.unread_origin }));
  const eventsFor = (id: string) => nativeEvents.get(id) ?? [{ id: `${id}-answer`, event_type: 'MESSAGE', payload: { source: 'agent', content: `${id} 的正式独立历史`, final_reply: true } }];
  const hydration = (id: string) => ({
    events: { events: eventsFor(id), next_cursor: eventsFor(id).at(-1)?.id, history_cursor: null },
    context: {}, readiness: { ready: true, execution_status: statuses.get(id) ?? 'finished' },
  });
  const signal = (id: string) => streams.get(id)?.send(JSON.stringify({ type: 'durable_cursor', seq: Date.now() }));
  await page.routeWebSocket('**/stream*', socket => {
    const match = new URL(socket.url()).pathname.match(/agent-session-children\/[^/]+\/([^/]+)\/stream/);
    if (match) streams.set(match[1], socket);
  });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    requests.push(url.pathname + url.search);
    if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`);
    const user = { id: 'child-owner', username: 'child-user', role: 'SUPER_ADMIN', is_super_admin: true };
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, user); }
    if (path.includes('/agent-session-children/')) {
      const suffix = path.split('/agent-session-children/')[1].split('/');
      const childId = suffix[1];
      if (suffix.length === 1) return json(route, suffix[0] === parentId ? children.map(child => ({ ...child, unread: unread.has(child.id) })) : []);
      const payload = request.method() === 'POST' && suffix[2] !== 'attachments' ? request.postDataJSON() as Record<string, unknown> : {};
      if (suffix[2] === 'messages') {
        messages.push({ childId, payload });
        if (held.nextSend) { held.nextSend = false; await new Promise<void>(resolve => { held.release = resolve; }); }
        const id = `user-${payload.submission_id}`;
        if (!held.hideAccepted && !eventsFor(childId).some(event => event.id === id)) nativeEvents.set(childId, [...eventsFor(childId), { id, event_type: 'MESSAGE', payload: { source: 'user', content: payload.content, attachments: payload.attachments, _flowweave_submission_id: payload.submission_id } }]);
        statuses.set(childId, 'running');
        if (held.failSend) { held.failSend = false; statuses.set(childId, 'finished'); return json(route, { error: { code: 'RUNTIME_CHILD_OUTCOME_UNKNOWN', message: '发送结果待确认' } }, 504); }
        return json(route, { accepted: true, cursor: id }, 202);
      }
      if (suffix[2] === 'control') {
        childWrites.push({ childId, action: 'control', payload });
        if (payload.operation === 'interrupt') statuses.set(childId, 'paused');
        if (payload.operation === 'resume') statuses.set(childId, 'running');
        if (payload.operation === 'condense') nativeEvents.set(childId, [...eventsFor(childId), { id: `condense-${Date.now()}`, event_type: 'CONDENSATION_REQUESTED', payload: {} }]);
        return json(route, { accepted: true, task_id: payload.operation === 'condense' ? 'child-condense-task' : undefined }, 202);
      }
      if (suffix[2] === 'model') { childWrites.push({ childId, action: 'model', payload }); models.set(childId, String(payload.model_name)); return json(route, payload); }
      if (suffix[2] === 'attachments') {
        if (request.method() === 'DELETE') return route.fulfill({ status: 204 });
        return json(route, { filename: 'notes.txt', mime_type: 'text/plain', byte_size: 7, path: `/runtime/workspace/project/uploads/${childId}-notes.txt` }, 201);
      }
      if (suffix[2] === 'file') return route.fulfill({ status: 200, contentType: 'text/plain', body: `private file for ${childId}` });
      if (suffix[2] === 'result-reference') return json(route, { source_binding_id: childId, source_event_id: suffix[3], content: String(eventsFor(childId).find(event => event.id === suffix[3])?.payload.content ?? '') });
      if (suffix[2] === 'pending-confirmation') {
        if (suffix[3]) { childWrites.push({ childId, action: 'confirmation', payload }); statuses.set(childId, 'running'); return json(route, { accepted: true }); }
        return json(route, { pending: statuses.get(childId) === 'waiting_for_confirmation', pending_actions_digest: 'native-batch', actions: [{ digest: 'action', tool_name: 'terminal', summary: '执行终端命令' }] });
      }
      if (suffix[2] === 'credentials') {
        if (suffix[3]) childWrites.push({ childId, action: 'credentials', payload });
        return json(route, { credentials: [{ id: 'credential-one', name: '测试认证', sync_state: 'UNRECORDED', inherited_from_binding_id: parentId }] });
      }
      if (suffix[2] === 'unread') { unread.delete(childId); return json(route, { id: childId, unread: false, unread_origin: null }); }
      if (suffix[2] === 'hydration') {
        reads += 1;
        peak = Math.max(peak, reads);
        if (shouldDelay && childId === children[0].id) { shouldDelay = false; await new Promise<void>(resolve => { release = resolve; }); }
        await json(route, hydration(childId));
        reads -= 1;
        return;
      }
      if (suffix[2] === 'events') return json(route, { events: [], next_cursor: `${childId}-answer` });
      return json(route, { ...children.find(child => child.id === childId), working_directory: '/runtime/workspace/project', model_provider_id: providers[0].id, model_name: models.get(childId) ?? 'child-model', capabilities: ['CONTEXT', 'SKILL', 'MCP'].map(type => ({ id: `frozen-${type}`, capability_key: `子会话${type}`, capability_type: type })), write_available: true, streaming_callback_ready: true });
    }
    if (path.endsWith('/default') || path.endsWith('/agent-sessions/host')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') || path.endsWith('/agent-sessions')) return json(route, { items: parents, next_cursor: null });
    if (path.endsWith('/conversation-activity') || path.endsWith('/agent-sessions/activity')) return json(route, { conversations: [parents[0]], running_binding_ids: [], child_activity_parent_binding_ids: [parentId], child_activity: activity() });
    if (path.endsWith('/hydration')) { const id = path.split('/').at(-2)!; return json(route, { events: { events: statuses.get(id) === 'running' ? [{ id: `${id}-question`, event_type: 'MESSAGE', payload: { source: 'user', content: '父会话独立运行' } }] : [], next_cursor: null }, context: {}, readiness: { ready: true, execution_status: statuses.get(id) ?? 'finished' } }); }
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'finished' });
    if (path.endsWith('/context')) return json(route, {});
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, providers);
    if (path.endsWith('/user-configurable-capabilities')) return json(route, [{ id: 'frozen-SKILL', capability_type: 'SKILL', capability_key: '子会话SKILL', revision_number: 1, is_latest: false }, { id: 'latest-SKILL', capability_type: 'SKILL', capability_key: '子会话SKILL', revision_number: 2, is_latest: true }, { id: 'unrelated-SKILL', capability_type: 'SKILL', capability_key: '其他能力', is_latest: true }]);
    if (path.endsWith('/pinned-conversations') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') || path.includes('/agent-sessions/')) return json(route, parents.find(parent => path.endsWith(parent.id)) ?? parents[0]);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill('child-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto(`${root}/${parentId}`);
  await expect(page.getByRole('list', { name: '子会话列表', exact: true })).toBeVisible();
  return { writes, requests, root, unread, parents, messages, childWrites, statuses, nativeEvents, eventsFor, streams, signal, held, release: () => release?.(), hasPending: () => Boolean(release), peak: () => peak };
}
async function chooseChild(page: Page, childId: string) {
  const showCard = page.getByRole('button', { name: '显示悬浮信息卡' });
  if (await showCard.isVisible()) await showCard.click();
  const list = page.getByRole('list', { name: '子会话列表', exact: true });
  await list.locator(`[data-child-binding-id="${childId}"]`).click();
}
for (const host of ['workspace', 'node'] as const) {
  test(`child card opens independent titled side tabs and closes only views (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    const parent = page.locator(`[data-conversation-binding-id="${parentId}"]:visible`);
    await expect(parent.getByRole('img', { name: '会话正在运行', exact: true })).toHaveCount(0);
    await expect(parent).toContainText('子会话 · 1 个运行中');
    await chooseChild(page, children[0].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[0].display_title}` })).toContainText(`${children[0].id} 的正式独立历史`);
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
    await expect(page.getByRole('button', { name: '全屏查看工作区工具' })).toBeEnabled();
    await chooseChild(page, children[1].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toContainText(`${children[1].id} 的正式独立历史`);
    await expect.poll(() => state.unread.has(children[1].id)).toBe(false);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` }).getByRole('button', { name: '从此处分叉会话' })).toHaveCount(0);
    const heights = await page.locator('.agent-child-conversation-panel.active').evaluate(element => ({ pane: element.clientHeight, body: element.parentElement!.clientHeight }));
    expect(heights.pane).toBeGreaterThan(heights.body - 4);
    await page.screenshot({ path: test.info().outputPath('child-tabs.png') });
    await page.locator(`[data-conversation-binding-id="${otherParentId}"] .agent-workspace-conversation-select:visible`).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toHaveCount(0);
    await page.locator(`[data-conversation-binding-id="${parentId}"] .agent-workspace-conversation-select:visible`).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toBeVisible();
    expect(state.unread.has(children[2].id)).toBe(true);
    const tabs = page.getByRole('navigation', { name: '工作区页签' });
    await expect(tabs.getByRole('button', { name: children[0].display_title, exact: true })).toBeVisible();
    await tabs.getByRole('button', { name: children[0].display_title, exact: true }).dblclick();
    await expect(page.locator('.agent-workspace-drawer.fullscreen')).toHaveCount(1);
    await tabs.getByRole('button', { name: children[0].display_title, exact: true }).dblclick();
    await expect(page.locator('.agent-workspace-drawer.fullscreen')).toHaveCount(0);
    await tabs.getByRole('button', { name: `关闭${children[0].display_title}页签` }).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toBeVisible();
    await tabs.getByRole('button', { name: `关闭${children[1].display_title}页签` }).click();
    expect(state.writes.filter(write => /DELETE|\/control/.test(write))).toEqual([]);
    await expect(page.getByRole('list', { name: '子会话列表', exact: true })).toBeVisible();
    await chooseChild(page, children[0].id);
    await page.reload();
    await expect(page.getByRole('list', { name: '子会话列表', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: `关闭${children[0].display_title}页签` })).toHaveCount(0);
  });
  test(`child reads serialize across fast tab selection and reject late display (${host})`, async ({ page }) => {
    const state = await openParent(page, host, true);
    await chooseChild(page, children[0].id);
    await expect.poll(state.hasPending).toBe(true);
    await chooseChild(page, children[1].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toContainText('正在加载会话');
    state.release();
    const pane = page.getByRole('region', { name: `子会话 ${children[1].display_title}` });
    await expect(pane).toContainText(`${children[1].id} 的正式独立历史`);
    await expect(pane).not.toContainText(`${children[0].id} 的正式独立历史`);
    expect(state.peak()).toBe(1);
  });
}

for (const host of ['workspace', 'node'] as const) {
  test(`child summary previews two entries and opens the complete list in one detail tab (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    const list = page.getByRole('list', { name: '子会话列表', exact: true });
    await expect(list.locator('[data-child-binding-id]')).toHaveCount(2);
    await expect(list.locator('.agent-child-preview-icon')).toHaveCount(2);
    await expect(page.locator('.agent-workspace-children > .lucide-messages-square')).toHaveCount(1);
    await expect(page.getByRole('navigation', { name: '子会话分页' })).toHaveCount(0);
    for (const width of [1440, 1024]) {
      await page.setViewportSize({ width, height: 1000 });
      const dimensions = await page.locator('.agent-workspace-summary').evaluate(element => [element, element.querySelector('.agent-workspace-overview')!, element.querySelector('.agent-workspace-children')!].map(item => ({
        width: item.clientWidth, contentWidth: item.scrollWidth,
        height: item.clientHeight, contentHeight: item.scrollHeight,
      })));
      for (const item of dimensions) {
        expect(item.contentWidth).toBeLessThanOrEqual(item.width);
        expect(item.contentHeight).toBeLessThanOrEqual(item.height);
      }
      await page.screenshot({ path: test.info().outputPath(`child-preview-${width}.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '查看全部子会话', exact: true }).click();
    const details = page.getByRole('region', { name: '全部子会话', exact: true });
    await expect(details.locator('[data-child-binding-id]')).toHaveCount(8);
    await expect(details).toContainText(children[2].display_title);
    await expect(page.getByRole('dialog', { name: '子会话列表' })).toHaveCount(0);
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
    expect(state.unread.has(children[2].id)).toBe(true);
    const tabs = page.getByRole('navigation', { name: '工作区页签' });
    const showCard = page.getByRole('button', { name: '显示悬浮信息卡', exact: true });
    if (await showCard.isVisible()) await showCard.click();
    await page.getByRole('button', { name: '查看全部子会话', exact: true }).click();
    await expect(tabs.getByRole('button', { name: '子会话', exact: true })).toHaveCount(1);
    await tabs.getByRole('button', { name: '子会话', exact: true }).dblclick();
    await expect(page.locator('.agent-workspace-drawer.fullscreen')).toBeVisible();
    await details.locator(`[data-child-binding-id="${children[2].id}"]`).click();
    const child = page.getByRole('region', { name: `子会话 ${children[2].display_title}`, exact: true });
    await expect(child).toContainText(`${children[2].id} 的正式独立历史`);
    await expect(page.locator('.agent-workspace-drawer.fullscreen')).toBeVisible();
    await expect.poll(() => state.unread.has(children[2].id)).toBe(false);
    await tabs.getByRole('button', { name: '子会话', exact: true }).click();
    await expect(details).toBeVisible();
    await page.screenshot({ path: test.info().outputPath('child-detail.png') });
    await page.getByRole('button', { name: '退出全屏', exact: true }).click();
    await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
    await expect(list).not.toBeVisible();
    await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).click();
    await expect(list.locator('[data-child-binding-id]')).toHaveCount(2);
  });

  test(`child fullscreen keeps draft and history while configuration Escape closes only the modal (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await input.fill('全屏切换保留的子会话草稿');
    const tabs = page.getByRole('navigation', { name: '工作区页签' });
    const tab = tabs.getByRole('button', { name: children[0].display_title, exact: true });
    await tab.dblclick();
    const fullscreen = page.locator('.agent-workspace-drawer.fullscreen');
    await expect(fullscreen).toBeVisible();
    await expect(input).toHaveValue('全屏切换保留的子会话草稿');
    const child = page.getByRole('region', { name: `子会话 ${children[0].display_title}`, exact: true });
    await expect(child).toContainText(`${children[0].id} 的正式独立历史`);
    await child.getByRole('button', { name: '子会话配置', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '子会话配置', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: '子会话配置', exact: true })).toHaveCount(0);
    await expect(fullscreen).toBeVisible();
    await tabs.getByRole('button', { name: '父会话', exact: true }).click();
    await expect(child).not.toBeVisible();
    await tab.click();
    await expect(child).toBeVisible();
    await expect(input).toHaveValue('全屏切换保留的子会话草稿');
    await page.screenshot({ path: test.info().outputPath('child-fullscreen.png') });
    await page.keyboard.press('Escape');
    await expect(fullscreen).toHaveCount(0);
    await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
    await expect(fullscreen).toBeVisible();
    await tab.dblclick();
    await expect(fullscreen).toHaveCount(0);
    await expect(input).toHaveValue('全屏切换保留的子会话草稿');
    expect(state.messages).toHaveLength(0);
    expect(state.childWrites.filter(write => write.action === 'control')).toHaveLength(0);
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
  });
}

for (const host of ['workspace', 'node'] as const) {
  test(`child drafts retain text and attachments across sibling parent close and reload (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await input.fill('第一个子会话的草稿');
    await page.getByLabel('上传子会话附件', { exact: true }).setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('private') });
    await expect(page.locator('[aria-label="子会话附件"]')).toContainText('notes.txt');
    await chooseChild(page, children[1].id);
    await expect(input).toHaveValue('');
    await input.fill('第二个子会话的草稿');
    const tabs = page.getByRole('navigation', { name: '工作区页签' });
    await tabs.getByRole('button', { name: children[0].display_title, exact: true }).click();
    await expect(input).toHaveValue('第一个子会话的草稿');
    await page.getByRole('button', { name: 'notes.txt', exact: true }).click();
    const preview = page.getByRole('dialog', { name: '文件预览' });
    await expect(preview).toContainText(`private file for ${children[0].id}`);
    await expect(preview.getByRole('button', { name: '在文件标签页打开' })).toHaveCount(0);
    await preview.getByRole('button', { name: '关闭文件预览' }).click();
    expect(state.requests.some(path => path.includes(`/agent-session-children/${parentId}/${children[0].id}/file?`))).toBe(true);
    await page.locator(`[data-conversation-binding-id="${otherParentId}"] .agent-workspace-conversation-select:visible`).click();
    await page.locator(`[data-conversation-binding-id="${parentId}"] .agent-workspace-conversation-select:visible`).click();
    await expect(input).toHaveValue('第一个子会话的草稿');
    await tabs.getByRole('button', { name: `关闭${children[0].display_title}页签` }).click();
    await chooseChild(page, children[0].id);
    await expect(input).toHaveValue('第一个子会话的草稿');
    await page.reload();
    await chooseChild(page, children[0].id);
    await expect(input).toHaveValue('第一个子会话的草稿');
    await expect(page.locator('[aria-label="子会话附件"]')).toContainText('notes.txt');
    await expect(input).toBeEditable();
    await expect(page.getByRole('region', { name: `子会话 ${children[0].display_title}` })).toContainText('正式独立历史');
    await page.screenshot({ path: test.info().outputPath('child-composer.png') });
    expect(state.messages).toHaveLength(0);
  });

  test(`child submission uses one bubble and holds queue until formal acknowledgement (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    state.held.hideAccepted = true;
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await input.fill('第一条正式消息');
    await input.press('Enter');
    await expect.poll(() => state.messages.length).toBe(1);
    await expect(page.locator('.agent-child-conversation-panel .conversation-message.user')).toHaveCount(1);
    await input.fill('等待第一条确认后再发送');
    await input.press('Enter');
    await expect(page.getByRole('region', { name: '子会话消息队列' })).toContainText('等待第一条确认后再发送');
    state.statuses.set(children[0].id, 'finished');
    state.signal(children[0].id);
    await expect(page.getByRole('button', { name: '暂停子会话 Agent' })).toHaveCount(0);
    expect(state.messages).toHaveLength(1);
    const first = state.messages[0].payload;
    state.nativeEvents.set(children[0].id, [...state.eventsFor(children[0].id), { id: `user-${first.submission_id}`, event_type: 'MESSAGE', payload: { source: 'user', content: first.content } }, { id: 'queue-first-finished', event_type: 'MESSAGE', payload: { source: 'agent', content: '第一条处理完成', final_reply: true } }]);
    state.held.hideAccepted = false;
    state.signal(children[0].id);
    await expect.poll(() => state.messages.length).toBe(2);
    expect(state.messages[1].payload.content).toBe('等待第一条确认后再发送');
    await expect(page.locator('.agent-child-conversation-panel .conversation-message.user')).toHaveCount(2);
    expect(state.writes.filter(write => /conversations\/[^/]+\/messages$|agent-sessions\/[^/]+\/messages$/.test(write))).toEqual([]);
  });

  test(`child ambiguous retry preserves submission identity and late callbacks stay scoped (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    state.held.failSend = true;
    state.held.nextSend = true;
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await input.fill('原子提交的消息');
    await input.press('Enter');
    await expect.poll(() => Boolean(state.held.release)).toBe(true);
    await chooseChild(page, children[1].id);
    await expect(input).toBeEditable();
    await input.fill('保持第二个子会话');
    state.held.release!();
    await expect(input).toHaveValue('保持第二个子会话');
    await expect(page.locator('.agent-child-conversation-panel')).not.toContainText('发送结果待确认');
    await page.getByRole('navigation', { name: '工作区页签' }).getByRole('button', { name: children[0].display_title, exact: true }).click();
    const retry = page.getByRole('button', { name: '使用原提交身份重试子会话消息' });
    await expect(retry).toBeEnabled();
    await retry.click();
    await expect.poll(() => state.messages.length).toBe(2);
    expect(state.messages[1].payload.submission_id).toBe(state.messages[0].payload.submission_id);
    expect(state.messages.map(message => message.childId)).toEqual([children[0].id, children[0].id]);
    await expect(page.locator('.agent-child-conversation-panel .conversation-message.user')).toHaveCount(1);
  });

  test(`child Esc pauses only child and result quote appends to parent without sending (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    state.parents[0].execution_status = 'running';
    state.statuses.set(parentId, 'running');
    await page.reload();
    state.statuses.set(children[0].id, 'running');
    state.nativeEvents.set(children[0].id, [{ id: 'formal-parent-instruction', event_type: 'MESSAGE', payload: { source: 'user', content: '父会话要求核对', parent_instruction: { operation: 'send', action_id: 'parent-action', tool_call_id: 'parent-tool' } } }, ...state.eventsFor(children[0].id)]);
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await expect(page.locator('.agent-child-conversation-panel')).toContainText('来自父会话的指令');
    await input.focus();
    await input.fill('/');
    await input.press('Escape');
    await expect(page.getByRole('button', { name: '暂停子会话 Agent' })).not.toContainText('Esc');
    await input.press('Escape');
    await expect(page.getByRole('button', { name: '暂停子会话 Agent' })).toContainText('Esc');
    expect(state.childWrites).toHaveLength(0);
    await input.press('Escape');
    await expect(page.getByRole('button', { name: '继续子会话 Agent' })).toBeVisible();
    expect(state.childWrites).toEqual([{ childId: children[0].id, action: 'control', payload: { operation: 'interrupt' } }]);
    await page.getByRole('button', { name: '继续子会话 Agent' }).click();
    await expect(page.getByRole('button', { name: '暂停子会话 Agent' })).toBeVisible();
    const parentInput = page.getByRole('textbox', { name: '发送 Agent 消息' });
    await parentInput.fill('父会话现有草稿');
    await page.getByRole('button', { name: '引用到父会话' }).click();
    await expect(parentInput).toHaveValue('父会话现有草稿');
    await expect(page.getByRole('button', { name: `${children[0].id} 的正式独立历史`, exact: true })).toBeVisible();
    await page.getByRole('button', { name: `${children[0].id} 的正式独立历史`, exact: true }).click();
    await expect(page.getByRole('dialog', { name: '会话引用内容' })).toContainText(`${children[0].id} 的正式独立历史`);
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
    await page.getByRole('button', { name: '关闭会话引用内容' }).click();
    expect(state.messages).toHaveLength(0);
    expect(state.writes.filter(write => !write.includes('/agent-session-children/') && /\/control$|\/messages$/.test(write))).toEqual([]);
  });

  test(`child model credentials and tool confirmation target current child (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    await chooseChild(page, children[0].id);
    const pane = page.locator('.agent-child-conversation-panel');
    await expect(page.getByRole('textbox', { name: '发送子会话消息' })).toBeEditable();
    await pane.getByRole('button', { name: /打开模型与推理设置/ }).click();
    await page.getByRole('option', { name: '模型 child-model-two', exact: true }).click();
    await expect.poll(() => state.childWrites.filter(write => write.action === 'model').length).toBe(1);
    expect(state.childWrites[0].payload.model_name).toBe('child-model-two');
    await pane.getByRole('button', { name: '子会话配置', exact: true }).click();
    const config = page.getByRole('dialog', { name: '子会话配置', exact: true });
    await expect(config).toHaveClass(/session-configuration-editor/);
    await expect(config.locator('.agent-capability-frozen-badge')).toHaveCount(3);
    await expect(config.getByRole('button', { name: '子会话CONTEXT（已装配）', exact: true })).toBeDisabled();
    await expect(config.getByRole('button', { name: '子会话SKILL（已装配）', exact: true })).toBeDisabled();
    await expect(config.getByRole('button', { name: '子会话MCP（已装配）', exact: true })).toBeDisabled();
    await expect(config.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);
    await expect(config.getByText('其他能力', {exact:true})).toHaveCount(0);
    await expect(config.getByRole('button', { name: /同步 子会话SKILL/ })).toHaveCount(0);
    await config.screenshot({ path: test.info().outputPath('child-config-capabilities.png') });
    await config.getByRole('tab', { name: '认证', exact: true }).click();
    await expect(config.getByRole('button', { name: /测试认证/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(config.getByRole('button', { name: /测试认证/ })).toContainText('已继承');
    await expect(config.getByRole('button', { name: /测试认证/ })).toHaveAttribute('title', /同步后可确认当前认证版本/);
    await config.screenshot({ path: test.info().outputPath('child-config-credentials.png') });
    await config.getByRole('button', { name: '同步认证' }).click();
    await expect.poll(() => state.childWrites.filter(write => write.action === 'credentials').length).toBe(1);
    expect(state.childWrites.at(-1)?.payload).toEqual({ credential_ids: ['credential-one'] });
    await config.getByRole('button', { name: '关闭子会话配置' }).click();
    expect(state.requests.filter(path => /credential-sync|mcp-readiness|capability-collections/.test(path))).toEqual([]);
    expect(state.writes.filter(path => /capabilities/.test(path))).toEqual([]);
    await pane.getByRole('button', { name: '子会话配置', exact: true }).click();
    await page.keyboard.press('Escape');
    await expect(config).toHaveCount(0);
    await pane.getByRole('button', { name: '子会话配置', exact: true }).click();
    await page.locator('.agent-capability-backdrop').click({ position: { x: 5, y: 5 } });
    await expect(config).toHaveCount(0);
    state.statuses.set(children[0].id, 'waiting_for_confirmation');
    state.signal(children[0].id);
    const confirmation = page.getByRole('region', { name: '子会话工具执行确认' });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('textbox', { name: '子会话工具确认理由' }).fill('仅批准当前子会话');
    await confirmation.getByRole('button', { name: '批准整批' }).click();
    await expect.poll(() => state.childWrites.filter(write => write.action === 'confirmation').length).toBe(1);
    expect(state.childWrites.at(-1)?.payload).toEqual({ expected_pending_digest: 'native-batch', accept: true, reason: '仅批准当前子会话' });
    await chooseChild(page, children[1].id);
    await expect(page.getByRole('region', { name: '子会话工具执行确认' })).toHaveCount(0);
    expect(state.messages).toHaveLength(0);
    expect(state.childWrites.every(write => write.childId === children[0].id)).toBe(true);
  });

  test(`child condense keeps editable input and queues until formal completion (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    await chooseChild(page, children[0].id);
    const input = page.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await input.fill('/');
    await page.getByRole('option', { name: /压缩上下文/ }).click();
    await expect.poll(() => state.childWrites.length).toBe(1);
    expect(state.childWrites[0].payload).toEqual({ operation: 'condense' });
    await expect(page.getByRole('button', { name: '暂停子会话 Agent' })).toHaveCount(0);
    await expect(input).toBeEditable();
    await input.fill('压缩结束再发送');
    await input.press('Enter');
    await expect(page.getByRole('region', { name: '子会话消息队列' })).toContainText('压缩结束再发送');
    expect(state.messages).toHaveLength(0);
    state.nativeEvents.set(children[0].id, [...state.eventsFor(children[0].id), { id: 'child-condensed', event_type: 'CONDENSATION_COMPLETED', payload: {} }]);
    state.signal(children[0].id);
    await expect.poll(() => state.messages.length).toBe(1);
    expect(state.messages[0].payload.content).toBe('压缩结束再发送');
    expect(state.childWrites.filter(write => write.payload.operation === 'interrupt')).toHaveLength(0);
  });
}

for (const host of ['workspace', 'node'] as const) {
  test(`child annotations workspace drops and direct send reuse child-scoped composer (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    await chooseChild(page, children[0].id);
    const pane = page.locator('.agent-child-conversation-panel');
    const input = pane.getByRole('textbox', { name: '发送子会话消息' });
    await expect(input).toBeEditable();
    await pane.locator('.conversation-message.assistant p').evaluate(element => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
    });
    await pane.locator('.conversation-surface').dispatchEvent('pointerup', { pointerType: 'mouse', button: 0 });
    await page.getByRole('button', { name: '添加到会话', exact: true }).click();
    await pane.getByRole('textbox', { name: '子会话注释', exact: true }).fill('核对这个结果');
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData('application/x-flowweave-workspace-file-path', '/runtime/workspace/project/result.txt');
      return data;
    });
    await pane.locator('.agent-composer-input').dispatchEvent('drop', { dataTransfer: transfer });
    await expect(pane.getByRole('button', { name: 'result.txt', exact: true })).toBeVisible();
    state.statuses.set(children[0].id, 'running');
    state.signal(children[0].id);
    await expect(pane.getByRole('button', { name: '暂停子会话 Agent' })).toBeVisible();
    await input.fill('先处理引用');
    await input.press('Alt+Enter');
    await expect.poll(() => state.childWrites.filter(write => write.payload.operation === 'interrupt').length).toBe(1);
    await expect.poll(() => state.messages.length).toBe(1);
    const message = state.messages[0];
    expect(message.childId).toBe(children[0].id);
    expect(message.payload.workspace_references).toEqual([{ path: '/runtime/workspace/project/result.txt', kind: 'file', display_name: 'result.txt' }]);
    expect(message.payload.annotations).toEqual([expect.objectContaining({ anchor_kind: 'CONVERSATION_TEXT', anchor: { source_binding_id: children[0].id, event_id: `${children[0].id}-answer`, quote: `${children[0].id} 的正式独立历史`, compact_start: 0 }, comment: '核对这个结果' })]);
    await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('');
  });
}

test('late child quote preserves original parent draft and foreground completion clears only selected child visuals', async ({ page }) => {
  const state = await openParent(page, 'workspace');
  state.statuses.set(children[0].id, 'running');
  await chooseChild(page, children[0].id);
  const pane = page.locator('.agent-child-conversation-panel');
  await expect(pane.getByRole('button', { name: '暂停子会话 Agent' })).toBeVisible();
  await expect.poll(() => state.streams.has(children[0].id)).toBe(true);
  state.streams.get(children[0].id)!.send(JSON.stringify({ type: 'stream_closed', item_id: 'completed-native-message' }));
  await expect(pane.getByRole('button', { name: '暂停子会话 Agent' })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '工作区页签' }).getByRole('img', { name: '子会话正在运行' })).toHaveCount(0);
  expect(state.statuses.get(children[0].id)).toBe('running');
  let release: (() => void) | undefined;
  await page.route('**/result-reference/**', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await json(route, { source_binding_id: children[0].id, source_event_id: `${children[0].id}-answer`, content: '原父会话的正式结果' });
  });
  await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('原父草稿');
  await pane.getByRole('button', { name: '引用到父会话' }).click();
  await expect.poll(() => release).toBeTruthy();
  await page.locator(`[data-conversation-binding-id="${otherParentId}"] .agent-workspace-conversation-select:visible`).click();
  await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('另一个父草稿');
  release!();
  await expect(page.getByRole('button', { name: '原父会话的正式结果', exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('另一个父草稿');
  await page.locator(`[data-conversation-binding-id="${parentId}"] .agent-workspace-conversation-select:visible`).click();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('原父草稿');
  await expect(page.getByRole('button', { name: '原父会话的正式结果', exact: true })).toBeVisible();
  expect(state.messages).toHaveLength(0);
});

test('child history prepend and sibling return preserve the independent reading position', async ({ page }) => {
  const state = await openParent(page, 'workspace');
  const event = (prefix: string, index: number) => ({ id: `${prefix}-${index}`, event_type: 'MESSAGE', payload: { parent_id: `${prefix}-user-${index}`, source: 'agent', content: `${prefix} ${index}\n\n${'较长的正式消息内容用于验证阅读位置。'.repeat(30)}`, final_reply: true } });
  const messages = (prefix: string) => Array.from({ length: 20 }, (_, index) => [
    { id: `${prefix}-user-${index}`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: index > 0 ? `${prefix}-${index - 1}` : prefix === 'recent' ? 'older-19' : '__root__', content: `${prefix} task ${index}` } },
    event(prefix, index),
  ]).flat();
  const recent = messages('recent');
  let release: (() => void) | undefined;
  let historyCalls = 0;
  let latestCalls = 0;
  await page.route(`**/agent-session-children/${parentId}/${children[0].id}/hydration*`, route => { latestCalls += 1; return json(route, { events: { events: recent, next_cursor: 'recent-19', history_cursor: 'older-page' }, context: {}, readiness: { ready: true, execution_status: 'finished' } }); });
  await page.route(`**/agent-session-children/${parentId}/${children[0].id}/events*`, async route => {
    historyCalls += 1;
    await new Promise<void>(resolve => { release = resolve; });
    await json(route, { events: messages('older'), next_cursor: 'recent-19', history_cursor: null });
  });
  await chooseChild(page, children[0].id);
  const pane = page.locator('.agent-child-conversation-panel');
  const surface = pane.locator('.conversation-surface');
  await expect(pane.locator('[data-event-id="recent-19"]')).toBeVisible();
  await expect.poll(() => release).toBeTruthy();
  await surface.evaluate(element => { element.dispatchEvent(new WheelEvent('wheel', { deltaY: -50, bubbles: true })); element.scrollTop = 600; element.dispatchEvent(new Event('scroll')); });
  const relativeTop = () => pane.locator('[data-event-id="recent-2"]').evaluate(element => element.getBoundingClientRect().top - element.closest('.conversation-surface')!.getBoundingClientRect().top);
  const before = await relativeTop();
  release!();
  await expect(pane.locator('[data-event-id="older-19"]')).toHaveCount(1);
  await expect.poll(relativeTop).toBeCloseTo(before, 0);
  const scrollTop = await surface.evaluate(element => element.scrollTop);
  await chooseChild(page, children[1].id);
  await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toContainText('正式独立历史');
  await page.getByRole('navigation', { name: '工作区页签' }).getByRole('button', { name: children[0].display_title, exact: true }).click();
  await expect(pane.locator('[data-event-id="recent-2"]')).toHaveCount(1);
  await expect.poll(() => surface.evaluate(element => element.scrollTop)).toBeCloseTo(scrollTop, 0);
  state.signal(children[0].id);
  await expect.poll(() => latestCalls).toBeGreaterThan(2);
  expect(historyCalls).toBe(1);
});

for (const host of ['workspace', 'node'] as const) {
  test(`family deletion keeps parent and child entry through 202 failure retry reload and final confirmation (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    let deleting = false;
    let deleted = false;
    let deletionState = 'PENDING';
    let phase = 'CHILDREN';
    let completed = 0;
    let deleteCalls = 0;
    let release: (() => void) | undefined;
    const deletion = () => ({ task_id: 'same-family-task', state: deletionState, phase, child_binding_ids: children.map(child => child.id), completed_children: completed, failure_code: deletionState === 'DEAD' ? 'RUNTIME_UNAVAILABLE' : null });
    await page.route(`**/api/v1/**/${parentId}`, async route => {
      if (new URL(route.request().url()).pathname.includes('/agent-session-children/')) return route.fallback();
      if (route.request().method() === 'DELETE') {
        deleteCalls += 1;
        if (deleteCalls === 1) await new Promise<void>(resolve => { release = resolve; });
        deleting = true;
        deletionState = 'PENDING';
        Object.assign(state.parents[0], { lifecycle: 'DELETE_PENDING', write_available: false, deletion: deletion() });
        return json(route, { parent_binding_id: parentId, ...deletion() }, 202);
      }
      if (deleted) return json(route, { error: { code: 'AGENT_CONVERSATION_NOT_FOUND', message: 'not found' } }, 404);
      if (deleting) return json(route, { ...state.parents[0], lifecycle: 'DELETE_PENDING', write_available: false, deletion: deletion() });
      return route.fallback();
    });
    await chooseChild(page, children[0].id);
    await expect(page.getByRole('textbox', { name: '发送子会话消息' })).toBeEditable();
    await page.locator(`[data-conversation-binding-id="${parentId}"]:visible`).click({ button: 'right' });
    await page.getByRole('menuitem', { name: '删除', exact: true }).click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText('同时清理 8 个关联子会话');
    await expect(dialog).toContainText(children[0].id);
    await dialog.getByRole('button', { name: '确认删除' }).click();
    await expect.poll(() => release).toBeTruthy();
    await expect(page.locator(`[data-conversation-binding-id="${parentId}"]:visible`)).toBeVisible();
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
    release!();
    const progress = page.getByRole('status', { name: '父子会话删除进度' });
    await expect(progress).toContainText('正在删除父子会话');
    await expect(progress.getByRole('list', { name: '关联子会话清单' }).getByRole('listitem')).toHaveCount(8);
    await expect(page.getByRole('textbox', { name: '发送子会话消息' })).toBeDisabled();
    await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveCount(0);
    await expect(page.locator(`[data-conversation-binding-id="${parentId}"]:visible`)).toContainText('正在删除父子会话');
    await page.getByRole('button', { name: /查看活动会话/ }).click();
    await expect(page.getByRole('region', { name: '活动会话', exact: true })).toContainText('正在删除父子会话');
    await page.getByRole('button', { name: '返回工作区列表' }).click();
    const showCard = page.getByRole('button', { name: '显示悬浮信息卡' });
    if (await showCard.isVisible()) await showCard.click();
    await expect(page.getByRole('list', { name: '子会话列表', exact: true }).locator(`[data-child-binding-id="${children[0].id}"]`)).toBeDisabled();
    deletionState = 'DEAD';
    await expect(progress).toContainText('RUNTIME_UNAVAILABLE');
    await expect(progress.getByRole('button', { name: '重试原清理任务' })).toBeVisible();
    await page.reload();
    await expect(progress).toContainText('父子会话删除未完成');
    await progress.getByRole('button', { name: '重试原清理任务' }).click();
    await expect.poll(() => deleteCalls).toBe(2);
    await expect(progress).toContainText('正在删除父子会话');
    phase = 'FINAL';
    completed = 2;
    await expect(progress).toContainText('已确认清理 2 / 8 个子会话');
    await page.screenshot({ path: test.info().outputPath('family-deletion.png') });
    await page.locator(`[data-conversation-binding-id="${otherParentId}"] .agent-workspace-conversation-select:visible`).click();
    await expect(page).toHaveURL(`${state.root}/${otherParentId}`);
    deleted = true;
    await expect(page.locator(`[data-conversation-binding-id="${parentId}"]:visible`)).toHaveCount(0);
    await expect(page).toHaveURL(`${state.root}/${otherParentId}`);
    expect(state.messages).toHaveLength(0);
  });
}

for (const host of ['workspace', 'node'] as const) {
  test(`parent all filter includes frozen Context Agent and Hook like the child (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    const types = ['CONTEXT', 'AGENT_DEFINITION', 'HOOK'];
    const capabilities = types.map(type => ({id: `parent-${type}`, capability_type: type, capability_key: `父会话${type}`, digest: 'frozen-digest'}));
    await page.route(url => url.pathname.includes('/api/v1/') && (url.pathname.endsWith(`/conversations/${parentId}`) || url.pathname.endsWith(`/agent-sessions/${parentId}`)), route => json(route, {...state.parents[0], capabilities}));
    await page.reload();
    await page.getByRole('button', {name:'会话配置',exact:true}).click();
    const config = page.getByRole('dialog', {name:'会话配置',exact:true});
    for (const type of types) await expect(config.getByRole('button', {name:`父会话${type}（已装配）`,exact:true})).toBeDisabled();
    await expect(config.locator('.agent-capability-frozen-badge')).toHaveCount(3);
    await config.getByRole('button', {name:'只看已选',exact:true}).click();
    for (const type of types) await expect(config.getByText(`父会话${type}`, {exact:true})).toBeVisible();
    await config.getByRole('button', {name:'context',exact:true}).click();
    await expect(config.locator('.agent-capability-list button')).toHaveCount(1);
    await config.getByRole('button', {name:'all',exact:true}).click();
    await expect(config.locator('.agent-capability-list button')).toHaveCount(3);
    await config.screenshot({path:test.info().outputPath('parent-all-frozen.png')});
    await config.getByRole('button', {name:'关闭会话配置',exact:true}).click();
  });
}
