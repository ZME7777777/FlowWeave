import { spawn } from 'node:child_process';
import { chmod, mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type Route } from '@playwright/test';
import type { CaseBasics, CaseSessionConfig, CaseStage } from '../src/types';

const now = '2026-10-19T00:00:00Z';

async function openCasePanel(page: Page, name: '场景信息' | 'Agent 配置') {
  const close = page.getByRole('button', { name: '关闭场景面板', exact: true });
  if (await close.count()) await close.click();
  await page.getByRole('button', { name, exact: true }).click();
}

for (const failureStatus of [503, 504, 403]) {
  test(`workspace activity survives a route read failure ${failureStatus} and recovers without another click`, async ({ page }) => {
    await page.clock.install();
    await openWorkspace(page, [], true);
    let failed = false;
    let completed = false;
    let failureCount = 0;
    await page.route('**/conversation-activity*', async route => {
      const active = new URL(route.request().url()).searchParams.get('active_binding_id');
      if (failed && active === 'scheduled-conversation-2') {
        failureCount += 1;
        return json(route, { error: { code: failureStatus === 403 ? 'AUTHORIZATION_DENIED' : 'RUNTIME_AUXILIARY_READ_SATURATED', message: '状态暂不可用' } }, failureStatus);
      }
      return json(route, { running_binding_ids: completed ? [] : ['scheduled-conversation'] });
    });
    await page.getByRole('button', { name: '日报会话', exact: true }).click();
    await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
    // Enter a second route so the first conversation becomes a background row.
    failed = true;
    await page.getByRole('button', { name: '周报会话', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: failureStatus === 403 ? '会话状态暂时不可用' : '会话状态待更新' })).toBeVisible();
    const running = page.locator('[data-conversation-binding-id="scheduled-conversation"]').getByRole('img', { name: '会话正在运行', exact: true });
    if (failureStatus === 403) {
      await expect(running).toHaveCount(0);
      expect(failureCount).toBe(1);
    } else {
      await expect(running).toBeVisible();
      expect(failureCount).toBe(3);
    }
    failed = false;
    completed = true;
    await page.clock.fastForward(16_000);
    await expect(page.getByRole('status').filter({ hasText: '会话状态' })).toHaveCount(0);
    await expect(running).toHaveCount(0);
    await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation-2$/);
  });
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function settleWorkspaceLayout(page: Page) {
  await page.evaluate(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    const elements = document.querySelectorAll('.agent-workbench-app-shell, .agent-workspace-drawer, .agent-workspace-tool-shell, .agent-workspace-summary, .conversation-surface, .agent-composer-dock');
    await Promise.all([...elements].flatMap(element => element.getAnimations()).map(animation => animation.finished.catch(() => undefined)));
  });
}

async function openWorkspace(page: Page, events: unknown[] = [], includeSecondConversation = false, conversationTitle = '日报会话', scheduleOptions: { prompt?: string; occurrenceCount?: number; capabilityVersionIds?: string[]; credentialIds?: string[]; defaultCapabilityIds?: string[]; defaultCredentialIds?: string[] } = {}) {
  const writes: { method: string; id?: string; body: Record<string, unknown> }[] = [];
  let authenticated = false;
  const workspace = { id: 'schedule-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now };
  const conversation = {
    id: 'scheduled-conversation', display_title: conversationTitle, title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: 'idle', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', created_at: now, updated_at: now,
  };
  const conversations = includeSecondConversation ? [conversation, { ...conversation, id: 'scheduled-conversation-2', display_title: '周报会话' }] : [conversation];
  const schedules = [{ id: 'daily-schedule', workspace_id: workspace.id, binding_id: null, work_directory_id: 'automation-directory', work_directory_name: '自动化 · 每日项目检查', workspace_mode: 'AUTOMATION', conversation_mode: 'NEW', model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'medium', capability_version_ids: scheduleOptions.capabilityVersionIds ?? [], credential_ids: scheduleOptions.credentialIds ?? [], name: '每日项目检查', prompt: '请检查项目状态。', cron_expression: '0 9 * * 1-5', status: 'ACTIVE', next_run_at: '2026-10-20T01:00:00Z', last_run_at: now, row_version: 1, created_at: now, updated_at: now }];
  const capabilities = [
    { id: 'capability-1', capability_key: '日报技能', capability_type: 'SKILL', description: '汇总每日检查结果', filename: 'daily.md', is_latest: true },
    { id: 'capability-2', capability_key: '周报技能', capability_type: 'SKILL', description: '汇总每周检查结果', filename: 'weekly.md', is_latest: true },
  ];
  const sessionConfigurations = new Map<string, { capability_version_ids: string[]; credential_ids: string[] }>();
  const configurationScope = (request: import('@playwright/test').Request) => new URL(request.url()).searchParams.get('work_directory_id') ?? 'root';
  sessionConfigurations.set('root', {
    capability_version_ids: scheduleOptions.defaultCapabilityIds ?? [] as string[],
    credential_ids: scheduleOptions.defaultCredentialIds ?? ['credential-1'] as string[],
  });
  schedules[0].prompt = scheduleOptions.prompt ?? schedules[0].prompt;
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
    if (path.endsWith('/session-configuration')) {
      const scope = configurationScope(request);
      if (request.method() === 'PUT') sessionConfigurations.set(scope, request.postDataJSON());
      return json(route, sessionConfigurations.get(scope) ?? { capability_version_ids: [], credential_ids: [] });
    }
    if (path.endsWith('/conversation-management')) return json(route, { items: [], truncated: false });
    if (path.endsWith('/conversation-schedules') && request.method() === 'POST') {
      const body = request.postDataJSON();
      writes.push({ method: 'POST', body });
      const schedule = { ...schedules[0], ...body, id: 'created-schedule', row_version: 1 };
      schedules.push(schedule);
      return json(route, schedule);
    }
    if (path.includes('/conversation-schedules/') && request.method() === 'DELETE') {
      const id = path.split('/').at(-1)!;
      writes.push({ method: 'DELETE', id, body: {} });
      schedules.splice(schedules.findIndex(item => item.id === id), 1);
      return route.fulfill({ status: 204 });
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
    if (path.includes('/conversation-schedules/') && path.endsWith('/run-conversations')) {
      const params = new URL(request.url()).searchParams;
      const offset = Number(params.get('cursor') ?? 0), limit = Number(params.get('limit') ?? 20);
      const items = Array.from({ length: scheduleOptions.occurrenceCount ?? 5 }, (_, index) => ({ id: `delivery-${index + 1}`, binding_id: `scheduled-run-${index + 1}`, conversation_title: `每日项目检查 · 运行 ${index + 1}`, scheduled_for: now, latest_run_at: now, trigger_kind: 'SCHEDULED', state: 'STARTED', created_at: now }));
      return json(route, { items: items.slice(offset, offset + limit), next_cursor: offset + limit < items.length ? String(offset + limit) : null, total: items.length });
    }
    if (path.includes('/conversation-schedules/') && path.endsWith('/occurrences')) return json(route, Array.from({ length: scheduleOptions.occurrenceCount ?? 5 }, (_, index) => ({ id: `delivery-${index + 1}`, binding_id: index === 0 ? 'scheduled-run-1' : `scheduled-run-${index + 1}`, conversation_title: `每日项目检查 · 2026-10-${19 - index} 09:00`, scheduled_for: now, trigger_kind: 'SCHEDULED', state: 'STARTED', message_cursor: `event-${index + 1}`, created_at: now })));
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return json(route, { items: conversations, next_cursor: null });
    if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [] });
    if (path.endsWith('/hydration')) return json(route, { events: { events, next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: { used_tokens: 136664, window_tokens: 512000, view_event_count: 68, condenser_max_size: 50 }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [{ id: 'existing-directory', display_name: '已有项目目录', current_version: { working_directory: '/runtime/workspace/project/existing-directory' } }] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/model-providers')) return json(route, [{ id: 'provider-1', name: '测试供应商', connection_state: 'CONNECTED', available_for_nodes: true, models: [{ model_name: 'model-1', enabled: true, is_default: true, default_reasoning_effort: 'medium', supported_reasoning_efforts: ['low', 'medium', 'high'] }] }]);
    if (path.endsWith('/website-credentials')) return json(route, [{ id: 'credential-1', name: '测试认证', target_host: 'example.test', target_path: '/', include_subdomains: false, auth_type: 'TOKEN', has_username: false, has_secret: true, row_version: 1, environment_names: { token: 'TEST_TOKEN' }, created_at: now, updated_at: now }]);
    if (path.endsWith('/capabilities')) return json(route, capabilities);
    if (path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') && request.method() === 'GET') {
      const bindingId = path.match(/\/conversations\/([^/]+)/)?.[1];
      return json(route, conversations.find(item => item.id === bindingId) ?? { ...conversation, id: bindingId });
    }
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByLabel('账号').fill('schedule-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  return writes;
}

test('fresh navigation opens case space and creates a scenario after the branch merge', async ({ page }) => {
  await openWorkspace(page);
  const cases: Record<string, unknown>[] = [];
  await page.route('**/api/v1/cases**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith('/stage-templates')) return json(route, []);
    if (route.request().method() === 'POST' && pathname.endsWith('/cases')) {
      const payload = route.request().postDataJSON();
      const created = { ...payload, id: 'merged-case', state: 'ACTIVE', row_version: 1,
        stages: [], tasks: [], flow_runs: [], control_plans: [], decision_proposals: [],
        control_conversation_binding_id: null, created_at: now, updated_at: now };
      cases.push(created);
      return json(route, created, 201);
    }
    return json(route, cases);
  });
  await page.evaluate(() => localStorage.removeItem('flowweave-workbench'));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('button', { name: '场景空间', exact: true })).toHaveAttribute('class', /active/);
  await expect(page.locator('.scenario-side-head')).toContainText('场景列表');
  await expect(page.getByRole('heading', { name: '选择或创建一个场景', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新建场景', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '新建场景', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '新建 Case' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '场景工作区' })).toHaveCount(0);
  await page.getByLabel('名称', { exact: true }).fill('合并后的案例');
  await page.getByLabel('目标', { exact: true }).fill('验证案例功能与本地工作台集成。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.scenario-tree')).toContainText('合并后的案例');
  await expect(page.locator('.case-workspace-toolbar')).toContainText('合并后的案例');
  await expect(page.getByRole('navigation', { name: '场景工作区' })).toHaveCount(0);
  await expect(page.locator('.scenario-path')).toBeVisible();
  await expect(page.locator('.case-config-panel')).toHaveCount(0);
  await openCasePanel(page, 'Agent 配置');
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeVisible();
  await expect(page.getByText('工作区模式', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '创建协调会话', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '关闭场景面板' }).click();
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Agent 工作区导航' })).toBeVisible();
});

test('case detail saves materials workspace ordered run cards and model capabilities', async ({ page }) => {
  await openWorkspace(page);
  type MockCase = CaseBasics & { id: string; row_version: number; stages: Pick<CaseStage, 'id' | 'flow_run_id' | 'name' | 'execution_mode' | 'state' | 'position'>[]; session_config?: CaseSessionConfig };
  let item: MockCase | undefined;
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  let rejectOrder = false;
  let parentRunning = true;
  const caseCredentials = ['credential-1', 'credential-2'].map((id, index) => ({ id, name: `场景认证 ${index + 1}`, target_host: 'example.test', target_path: '/', auth_type: 'TOKEN', row_version: 1 }));
  await page.route('**/api/v1/website-credentials', route => json(route, caseCredentials));
  const templates = [1, 2, 3].map(index => ({ id: `run-${index}`, name: `流程记录 ${index}`, parent_flow_run_id: index === 3 ? 'master-2' : 'master-1', execution_mode: index === 1 ? 'STEPWISE' : 'CONTINUOUS', reachable_node_keys: ['start'] }));
  await page.route('**/api/v1/flow-runs', route => json(route, [
    { id: 'master-1', name: '客户迁移', run_no: 1, state: 'ACTIVE', runtime_status: parentRunning ? 'ACTIVE' : 'STOPPED', runtime_write_available: parentRunning },
    { id: 'master-2', name: '数据归档', run_no: 2, state: 'ACTIVE', runtime_status: 'ACTIVE', runtime_write_available: true },
    { id: 'empty-master', name: '待配置流程', run_no: 3, state: 'ACTIVE', runtime_status: 'ACTIVE', runtime_write_available: true },
    ...['COMPLETED', 'CANCELLED', 'FAILED', 'WAITING_HUMAN', 'DRAFT'].map(state => ({ id: state, name: `非运行记录 ${state}`, state, run_no: 4, runtime_status: 'ACTIVE', runtime_write_available: true })),
    ...['STOPPED', 'STARTING', 'RECONNECTING', 'DEGRADED'].map(runtime_status => ({ id: runtime_status, name: `不可用运行环境 ${runtime_status}`, state: 'ACTIVE', run_no: 5, runtime_status, runtime_write_available: false })),
  ]));
  await page.route('**/api/v1/cases**', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.endsWith('/stage-templates')) return json(route, templates.filter(template => !item?.stages.some(stage => stage.flow_run_id === template.id)));
    if (path.endsWith('/materials/file')) return route.fulfill({ status: 200, contentType: 'text/plain', body: 'case inputs' });
    if (method === 'GET') return json(route, item ? [item] : []);
    if (path.endsWith('/materials')) {
      expect(route.request().headers()['content-type']).toContain('multipart/form-data');
      const filename = route.request().postDataBuffer()!.toString('utf8').match(/filename="([^"]+)"/)![1];
      expect(filename).toBe('input.txt');
      item = { ...item, materials: [...item!.materials, { kind: 'FILE', name: filename, value: '.flowweave/cases/config-case/materials/file-1' }], row_version: item!.row_version + 1 };
      return json(route, item, 201);
    }
    const body = route.request().postDataJSON();
    writes.push({ path, body });
    if (method === 'POST') {
      item = { ...body, id: 'config-case', row_version: 1, stages: [], state: 'DRAFT', control_work_directory_id: 'case-directory', tasks: [], control_plans: [], decision_proposals: [] };
      return json(route, item, 201);
    }
    expect(body.expected_row_version).toBe(item!.row_version);
    if (path.endsWith('/path')) {
      if (rejectOrder) return json(route, { error: { code: 'VERSION_CONFLICT', message: '路径已更新，请重试' } }, 409);
      item = { ...item, stages: body.cards.map((card: { id?: string; flow_run_id?: string }, index: number) => card.id
        ? { ...item!.stages.find(stage => stage.id === card.id), position: index }
        : { id: `stage-${card.flow_run_id}`, flow_run_id: card.flow_run_id, name: templates.find(template => template.id === card.flow_run_id)!.name, execution_mode: templates.find(template => template.id === card.flow_run_id)!.execution_mode, state: 'PENDING', position: index }), row_version: item!.row_version + 1 };
    } else if (path.endsWith('/session-config')) {
      const config = { ...body };
      delete config.expected_row_version;
      item = { ...item, session_config: config, row_version: item!.row_version + 1 };
    } else item = { ...item, ...body, row_version: item!.row_version + 1 };
    return json(route, item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '新建场景' }).click();
  await page.getByLabel('名称', { exact: true }).fill('迁移准备');
  await page.getByLabel('说明', { exact: true }).fill('客户迁移的基础说明');
  await page.getByLabel('目标', { exact: true }).fill('完成上线准备');
  const workspace = page.getByRole('combobox', { name: '工作区', exact: true });
  await workspace.fill('不存在的目录');
  await expect(page.getByRole('listbox', { name: '工作区' })).toContainText('没有匹配的选项');
  await workspace.fill('已有项目');
  await expect(page.getByRole('option', { name: '默认工作区', exact: true })).toHaveCount(0);
  await workspace.press('ArrowDown');
  await workspace.press('Enter');
  await expect(workspace).toHaveValue('已有项目目录');
  await workspace.click();
  await workspace.fill('未选中的查询');
  await workspace.press('Escape');
  await expect(workspace).toHaveValue('已有项目目录');
  await workspace.fill('默认');
  await page.getByRole('heading', { name: '新建 Case', exact: true }).click();
  await expect(workspace).toHaveValue('已有项目目录');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '添加资料', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.scenario-path')).toBeVisible();
  await openCasePanel(page, '场景信息');
  await page.getByRole('button', { name: '添加资料', exact: true }).click();
  let materialDialog = page.getByRole('dialog', { name: '添加资料', exact: true });
  await materialDialog.getByLabel('资料名称').fill('需求文档');
  await materialDialog.getByLabel('资料地址').fill('https://example.test/input');
  await materialDialog.getByRole('button', { name: '添加', exact: true }).click();
  expect(writes).toHaveLength(1);
  await page.getByRole('button', { name: '添加资料', exact: true }).click();
  materialDialog = page.getByRole('dialog', { name: '添加资料', exact: true });
  await materialDialog.getByLabel('资料名称', { exact: true }).fill('准备资料.txt');
  await materialDialog.getByLabel('导入资料文件').setInputFiles({ name: 'input.txt', mimeType: 'text/plain', buffer: Buffer.from('case inputs') });
  await expect(materialDialog.getByLabel('资料地址')).toHaveValue('.flowweave/cases/config-case/materials/file-1');
  await materialDialog.getByRole('button', { name: '添加', exact: true }).click();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.scenario-path')).toBeVisible();
  expect(item!.description).toBe('客户迁移的基础说明');
  expect(item!.work_directory_id).toBe('existing-directory');
  expect(item!.materials).toHaveLength(2);
  await openCasePanel(page, '场景信息');
  await expect(page.locator('.scenario-materials')).toContainText('准备资料.txt');
  await page.getByRole('button', { name: '重命名 准备资料.txt', exact: true }).click();
  await page.getByRole('textbox', { name: '资料名称 准备资料.txt', exact: true }).fill('上线资料');
  await page.getByRole('textbox', { name: '资料名称 准备资料.txt', exact: true }).press('Enter');
  await page.getByRole('button', { name: '重命名 需求文档', exact: true }).click();
  await page.getByRole('textbox', { name: '资料名称 需求文档', exact: true }).fill('迁移需求');
  await page.getByRole('button', { name: '确认改名', exact: true }).click();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.case-inspector').getByRole('status')).toHaveText('已保存');
  expect(item!.materials.map(material => material.name)).toEqual(['迁移需求', '上线资料.txt']);
  await page.getByRole('button', { name: '上线资料.txt', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '资料预览' })).toContainText('case inputs');
  await page.getByRole('button', { name: '关闭预览' }).click();
  const header = await page.locator('.scenario-side-head').boundingBox();
  expect(header!.height).toBeLessThanOrEqual(56);
  await page.getByRole('button', { name: '关闭场景面板', exact: true }).click();
  await expect(page.locator('.scenario-path ol li')).toHaveCount(0);
  await page.screenshot({ path: '../../.tmp/ops35/path-empty.png' });
  for (const index of [1, 2]) {
    await page.getByRole('button', { name: /添加.*流程/ }).click();
    await expect(page.getByRole('combobox', { name: '运行方式', exact: true })).toBeDisabled();
    await expect(page.getByRole('combobox', { name: '运行记录', exact: true })).toBeDisabled();
    await page.getByRole('combobox', { name: 'FlowRun', exact: true }).click();
    await expect(page.getByRole('option', { name: /非运行记录|不可用运行环境/ })).toHaveCount(0);
    await page.getByRole('option', { name: '客户迁移', exact: true }).click();
    await page.getByRole('combobox', { name: '运行方式', exact: true }).click();
    await page.getByRole('option', { name: index === 1 ? '逐步运行' : '连续运行', exact: true }).click();
    await page.getByRole('combobox', { name: '运行记录', exact: true }).click();
    await expect(page.getByRole('option', { name: '流程记录 3', exact: true })).toHaveCount(0);
    await page.getByRole('option', { name: `流程记录 ${index}`, exact: true }).click();
    if (index === 1) {
      // Changing an ancestor cannot submit a record from the previous owner or mode.
      await page.getByRole('combobox', { name: 'FlowRun', exact: true }).fill('待配置');
      await page.getByRole('option', { name: '待配置流程', exact: true }).click();
      await expect(page.getByRole('combobox', { name: '运行方式', exact: true })).toHaveValue('');
      await expect(page.getByRole('combobox', { name: '运行记录', exact: true })).toHaveValue('');
      await expect(page.locator('.scenario-add-card').getByRole('button', { name: '添加', exact: true })).toBeDisabled();
      await page.getByRole('combobox', { name: '运行方式', exact: true }).click();
      await page.getByRole('option', { name: '连续运行', exact: true }).click();
      await expect(page.locator('.scenario-add-card')).toContainText('暂无可添加的连续运行记录');
      await page.getByRole('combobox', { name: 'FlowRun', exact: true }).click();
      await page.getByRole('option', { name: '客户迁移', exact: true }).click();
      await page.getByRole('combobox', { name: '运行方式', exact: true }).click();
      await page.getByRole('option', { name: '逐步运行', exact: true }).click();
      await page.getByRole('combobox', { name: '运行记录', exact: true }).click();
      await page.getByRole('option', { name: '流程记录 1', exact: true }).click();
      await page.getByRole('combobox', { name: '运行记录', exact: true }).click();
      for (const width of [1440, 1024]) {
        await page.setViewportSize({ width, height: 768 });
        const picker = page.locator('.case-path-picker');
        expect((await picker.boundingBox())!.height).toBeLessThan(125);
        const controls = await picker.getByRole('combobox').all();
        const boxes = await Promise.all(controls.map(control => control.boundingBox()));
        expect(Math.round(boxes[0]!.y)).toBe(Math.round(boxes[2]!.y));
        expect(await picker.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
        await expect(page.getByRole('option', { name: '流程记录 1', exact: true })).toContainText('run-1');
        await page.screenshot({ path: `../../.tmp/ops35/picker-open-${width}.png` });
      }

      await page.getByRole('combobox', { name: '运行记录', exact: true }).press('Escape');
      await page.screenshot({ path: '../../.tmp/ops35/picker.png' });
      parentRunning = false;
      await expect(page.locator('.scenario-add-card')).toContainText('所选 FlowRun 已停止运行，请重新选择。');
      await expect(page.locator('.scenario-add-card').getByRole('button', { name: '添加', exact: true })).toBeDisabled();
      await expect(page.getByRole('combobox', { name: '运行记录', exact: true })).toBeDisabled();
      parentRunning = true;
      await expect(page.locator('.scenario-add-card').getByRole('button', { name: '添加', exact: true })).toBeEnabled();
    }
    await page.locator('.scenario-add-card').getByRole('button', { name: '添加', exact: true }).click();
    await expect(page.locator('.scenario-add-card')).toHaveCount(0);
  }
  const first = page.getByRole('article', { name: '路径卡片 流程记录 1', exact: true });
  const second = page.getByRole('article', { name: '路径卡片 流程记录 2', exact: true });
  rejectOrder = true;
  await second.dragTo(first, { targetPosition: { x: 30, y: 5 } });
  await expect(page.getByRole('alert')).toContainText('路径已更新，请重试');
  await expect(page.locator('.scenario-path li').first()).toContainText('流程记录 1');
  rejectOrder = false;
  await second.dragTo(first, { targetPosition: { x: 30, y: 5 } });
  await expect(page.locator('.scenario-path li').first()).toContainText('流程记录 2');
  await page.screenshot({ path: '../../.tmp/ops35/path.png' });
  // Keyboard reordering uses the same persisted order.
  await page.getByRole('button', { name: '调整顺序 流程记录 2', exact: true }).press('ArrowDown');
  await expect(page.locator('.scenario-path li').first()).toContainText('流程记录 1');
  await page.getByRole('button', { name: '调整顺序 流程记录 2', exact: true }).press('ArrowUp');
  await expect(page.locator('.scenario-path li').first()).toContainText('流程记录 2');
  await page.getByRole('button', { name: '删除卡片 流程记录 1' }).click();
  await expect(page.locator('.scenario-path li')).toHaveCount(1);
  await openCasePanel(page, 'Agent 配置');
  await expect(page.getByRole('combobox', { name: '模型名称', exact: true })).toBeDisabled();
  await page.getByRole('combobox', { name: '模型供应商', exact: true }).click();
  await page.getByRole('option', { name: '测试供应商', exact: true }).click();
  await page.getByRole('combobox', { name: '模型名称', exact: true }).click();
  await page.getByRole('option', { name: 'model-1', exact: true }).click();
  await page.getByRole('combobox', { name: '思考程度', exact: true }).click();
  await page.getByRole('option', { name: 'high', exact: true }).click();
  await page.getByRole('button', { name: '配置会话', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
  await dialog.getByRole('button', { name: /日报技能/ }).click();
  await dialog.getByRole('button', { name: '保存会话配置', exact: true }).click();
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.locator('.case-inspector').getByRole('status')).toContainText('已保存');
  expect(item!.session_config).toMatchObject({ model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', capability_version_ids: ['capability-1'], credential_ids: ['credential-1', 'credential-2'] });
  await openCasePanel(page, '场景信息');
  await openCasePanel(page, 'Agent 配置');
  await expect(page.getByRole('combobox', { name: '思考程度', exact: true })).toHaveValue('high');
  for (const count of [1, 0]) {
    await page.getByRole('button', { name: '配置会话', exact: true }).click();
    const credentialDialog = page.getByRole('dialog', { name: '会话配置', exact: true });
    await credentialDialog.getByRole('button', { name: '认证', exact: true }).click();
    await expect(credentialDialog.getByRole('button', { name: /场景认证 1/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(credentialDialog.getByRole('button', { name: /场景认证 2/ })).toHaveAttribute('aria-pressed', count === 1 ? 'true' : 'false');
    await credentialDialog.getByRole('button', { name: count === 1 ? /场景认证 2/ : /场景认证 1/ }).click();
    await credentialDialog.getByRole('button', { name: '保存认证配置', exact: true }).click();
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect(page.locator('.case-inspector').getByRole('status')).toHaveText('已保存');
    expect(item!.session_config!.credential_ids).toEqual(count === 1 ? ['credential-1'] : []);
    await openCasePanel(page, '场景信息');
    await openCasePanel(page, 'Agent 配置');
  }
  await page.getByRole('button', { name: '配置会话', exact: true }).click();
  await page.getByRole('dialog', { name: '会话配置', exact: true }).getByRole('button', { name: '认证', exact: true }).click();
  await expect(page.getByRole('button', { name: /场景认证 1/ })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByRole('button', { name: /场景认证 2/ })).toHaveAttribute('aria-pressed', 'false');
  await page.getByRole('button', { name: '关闭会话配置', exact: true }).click();

  await expect(page.getByText('Case 会话系统上下文', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: '../../.tmp/ops35/conversation.png' });
  await page.getByRole('button', { name: '关闭场景面板', exact: true }).click();
  await page.screenshot({ path: '../../.tmp/ops35/path-single.png' });
  await openCasePanel(page, '场景信息');
  const renameButtons = await page.locator('.case-material-rename-button').all();
  const renameBoxes = await Promise.all(renameButtons.map(button => button.boundingBox()));
  expect(renameBoxes[0]!.x).toBe(renameBoxes[1]!.x);
  await page.screenshot({ path: '../../.tmp/ops35/basics.png' });
  await page.setViewportSize({ width: 1024, height: 900 });
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeVisible();
  expect(await page.locator('.case-inspector').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: '../../.tmp/ops35/basics-1024.png' });
  await expect(page.locator('.scenario-workspace select')).toHaveCount(0);
  await openCasePanel(page, 'Agent 配置');
  const controls = await page.locator('.case-session-grid .case-choice-control, .case-capability-choice').all();
  const boxes = await Promise.all(controls.map(control => control.boundingBox()));
  expect(boxes[0]!.width).toBeCloseTo(boxes[1]!.width, 0);
  expect(boxes[2]!.height).toBe(boxes[3]!.height);
  expect(boxes[2]!.y).toBe(boxes[3]!.y);
  await page.screenshot({ path: '../../.tmp/ops35/conversation-1024.png' });
  expect(writes.filter(write => write.path.endsWith('/cases'))).toHaveLength(1);
});

test('case default credentials block saving on catalog failure and select all after retry', async ({ page }) => {
  await openWorkspace(page);
  let fail = true;
  const writes: Record<string, unknown>[] = [];
  const item = { id: 'credential-case', name: '默认认证场景', row_version: 1, state: 'DRAFT', stages: [], tasks: [], control_plans: [], decision_proposals: [],
    session_config: { model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', capability_version_ids: [] } };
  await page.route('**/api/v1/cases**', route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      writes.push(body);
      return json(route, { ...item, session_config: body, row_version: 2 });
    }
    return json(route, [item]);
  });
  await page.route('**/api/v1/website-credentials', route => fail
    ? json(route, { error: { code: 'SERVICE_UNAVAILABLE', message: '认证列表读取失败' } }, 503)
    : json(route, [{ id: 'credential-a', name: '认证 A' }, { id: 'credential-b', name: '认证 B' }]));
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await openCasePanel(page, 'Agent 配置');
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '配置会话', exact: true })).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('认证列表读取失败');
  expect(writes).toEqual([]);
  fail = false;
  await page.getByRole('button', { name: '重新读取认证', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.locator('.case-inspector').getByRole('status')).toHaveText('已保存');
  expect(writes[0].credential_ids).toEqual(['credential-a', 'credential-b']);
});

for (const [width, height] of [[1440, 900], [1440, 700], [1024, 768], [1280, 620]]) {
  test(`case basics stays fixed with empty and long material lists at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await openWorkspace(page);
    const item = { id: 'compact-case', name: '紧凑场景', description: '完整背景说明。'.repeat(200), objective: '完整目标。'.repeat(200), state: 'DRAFT', row_version: 1,
      materials: Array.from({ length: 100 }, (_, index) => ({ kind: 'LINK', name: `资料 ${index + 1}`, value: `https://example.test/input/${index}` })),
      stages: [], tasks: [], control_plans: [], decision_proposals: [], created_at: now, updated_at: now };
    await page.route('**/api/v1/cases', route => json(route, [item]));
    await page.getByRole('button', { name: '场景空间', exact: true }).click();
    const assertFixed = async () => {
      const boxes = await page.locator('.scenario-space, .case-basics-workspace, .case-inspector').evaluateAll(elements => elements.map(element => ({ height: element.clientHeight, scroll: element.scrollHeight, width: element.clientWidth, scrollWidth: element.scrollWidth })));
      expect(boxes.every(box => box.scroll <= box.height + 1 && box.scrollWidth <= box.width + 1)).toBe(true);
      const save = await page.getByRole('button', { name: '保存', exact: true }).boundingBox();
      expect(save!.y + save!.height).toBeLessThanOrEqual(height - 8);
      await page.locator('.case-basics-fields').hover(); await page.mouse.wheel(0, 800);
      expect(await page.locator('.case-inspector, .case-basics-workspace').first().evaluate(element => element.scrollTop)).toBe(0);
      expect((await page.locator('.topbar').boundingBox())!.y).toBe(0);
      expect(await page.locator('html').evaluate(element => getComputedStyle(element).overscrollBehaviorY)).toBe('none');
      expect(await page.evaluate(() => ({ scroll: document.documentElement.scrollHeight, height: innerHeight, top: scrollY }))).toEqual({ scroll: height, height, top: 0 });
    };
    await openCasePanel(page, '场景信息');
    await expect(page.getByRole('textbox', { name: '说明', exact: true })).toHaveValue(item.description);
    await assertFixed();
    await expect(page.locator('.scenario-materials ul')).toBeVisible();
    expect(await page.locator('.scenario-materials ul').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
    await page.locator('.scenario-materials ul').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(page.getByRole('link', { name: '资料 100', exact: true })).toBeInViewport();
    await page.screenshot({ path: `../../.tmp/ops33/basics-materials-${width}-${height}.png` });
    await page.getByRole('button', { name: '关闭场景面板', exact: true }).click();
    await page.getByRole('button', { name: '新建场景', exact: true }).click();
    await assertFixed();
    await expect(page.getByRole('button', { name: '添加资料', exact: true })).toHaveCount(0);
    await expect(page.locator('.scenario-materials')).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: '说明', exact: true })).toBeEditable();
    await expect(page.getByRole('textbox', { name: '目标', exact: true })).toBeEditable();
    await page.screenshot({ path: `../../.tmp/ops33/basics-new-${width}-${height}.png` });
  });
}

test('case deletion confirms intent and retains the case when the server rejects it', async ({ page }) => {
  await openWorkspace(page);
  let deleted = false;
  let rejectDelete = true;
  let deleteCount = 0;
  const item = { id: 'delete-case', name: '删除保护场景', objective: '', state: 'ACTIVE', row_version: 1,
    stages: [], tasks: [], flow_runs: [], control_plans: [], decision_proposals: [],
    control_conversation_binding_id: null, created_at: now, updated_at: now };
  await page.route('**/api/v1/cases**', async route => {
    if (route.request().method() === 'DELETE') {
      deleteCount += 1;
      if (rejectDelete) return json(route, { error: { code: 'CASE_EXECUTION_RUNNING', message: '事项仍有进行中的执行，停止后才能删除' } }, 409);
      deleted = true;
      return route.fulfill({ status: 204 });
    }
    return json(route, deleted ? [] : [item]);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '删除场景', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '取消', exact: true }).click();
  expect(deleteCount).toBe(0);
  await page.getByRole('button', { name: '删除场景', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除 Case', exact: true }).click();
  await expect(page.locator('.scenario-delete-error')).toContainText('事项仍有进行中的执行');
  await expect(page.locator('.case-workspace-toolbar')).toContainText(item.name);
  rejectDelete = false;
  await page.getByRole('button', { name: '删除场景', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除 Case', exact: true }).click();
  await expect(page.getByRole('heading', { name: '选择或创建一个场景', exact: true })).toBeVisible();
  expect(deleteCount).toBe(2);
});

test('schedule deletion removes only its task and keeps existing conversations', async ({ page }) => {
  const writes = await openWorkspace(page);
  await page.getByRole('button', { name: '定时任务', exact: true }).click();
  await page.locator('.agent-schedule-tree-select').filter({ hasText: '每日项目检查' }).click();
  await page.locator('.agent-schedule-actions').getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '取消', exact: true }).click();
  expect(writes.filter(item => item.method === 'DELETE')).toHaveLength(0);
  await page.locator('.agent-schedule-actions').getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除任务', exact: true }).click();
  await expect(page.locator('.agent-schedule-tree')).not.toContainText('每日项目检查');
  await expect(page.locator('.agent-schedule-tree')).toContainText('每周复核');
  expect(writes.filter(item => item.method === 'DELETE')).toEqual([{ method: 'DELETE', id: 'daily-schedule', body: {} }]);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '主页', exact: true }).click();
  await expect(page.getByRole('button', { name: '日报会话', exact: true })).toBeVisible();
});

test('conversation images resolve in the bound user project and preview with the current binding', async ({ page }) => {
  const root = '/runtime/workspace/project/users/00000000-0000-0000-0000-000000000001/my-project';
  const relative = 'artifacts/browser/native-conversation/前端截图.png';
  const imagePath = `${root}/${relative}`;
  const fileRoute = '/api/v1/agent-workspaces/schedule-workspace/workspace/file';
  const staleUrl = `${fileRoute}?path=${encodeURIComponent(imagePath)}&binding_id=stale-binding`;
  await openWorkspace(page, [
    { id: 'image-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看截图', timestamp: now } },
    { id: 'image-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'image-user', content: [
      `![相对截图](${relative})`,
      `![沙箱截图](sandbox:${imagePath})`,
      `![绝对截图](${imagePath})`,
      `![历史接口截图](${staleUrl})`,
      '![其他用户](sandbox:/runtime/workspace/project/users/00000000-0000-0000-0000-000000000002/secret.png)',
      '![内部状态](sandbox:/runtime/state/conversations/private.png)',
      '![越界截图](../../outside.png)',
    ].join('\n\n'), timestamp: now } },
  ]);
  await page.route('**/work-directories', route => json(route, { root: { kind: 'ROOT', display_name: '用户工作区', working_directory: root }, items: [] }));
  await page.route(/\/workspace(?:\?.*)?$/, route => json(route, { root, scope: { kind: 'ROOT', display_name: '当前项目' }, working_directory: root, files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } }));
  const requests: { path: string | null; binding: string | null }[] = [];
  await page.route('**/workspace/file?**', route => {
    const url = new URL(route.request().url());
    requests.push({ path: url.searchParams.get('path'), binding: url.searchParams.get('binding_id') });
    return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6HwAAAABJRU5ErkJggg==', 'base64') });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  for (const label of ['相对截图', '沙箱截图', '绝对截图', '历史接口截图']) {
    const image = page.getByRole('img', { name: label, exact: true });
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBe(1);
    const src = new URL((await image.getAttribute('src'))!, test.info().project.use.baseURL);
    expect(src.pathname).toBe(fileRoute);
    expect(src.searchParams.get('path')).toBe(imagePath);
    expect(src.searchParams.get('binding_id')).toBe('scheduled-conversation');
  }
  await expect(page.locator('.conversation-image-unavailable')).toHaveCount(3);
  await page.getByRole('button', { name: '预览图片：沙箱截图', exact: true }).click();
  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview).toBeVisible();
  await expect(preview.getByRole('img', { name: '沙箱截图', exact: true })).toBeVisible();
  expect(requests.length).toBeGreaterThan(0);
  expect(requests.every(item => item.path === imagePath && item.binding === 'scheduled-conversation')).toBe(true);
  await page.screenshot({ path: '../../.tmp/fr666-workspace-image-preview.png' });
});

test('workspace Git settings show only confirmed identity and save with readback', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openWorkspace(page);
  await page.route('**/api/v1/auth/profile', route => json(route, { display_name: '账户姓名', email: 'account@example.test' }));
  let identity = { author: 'Login Profile', email: 'login@example.test', configured: false };
  let reads = 0;
  let failSave = true;
  let releaseRead: () => void = () => undefined;
  const firstRead = new Promise<void>(resolve => { releaseRead = resolve; });
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/settings/git', async route => {
    if (route.request().method() === 'GET') {
      reads += 1;
      if (reads === 1) await firstRead;
      return json(route, identity);
    }
    if (failSave) return json(route, { error: { code: 'GIT_FAILED', message: '写入未完成' } }, 503);
    identity = { ...route.request().postDataJSON(), configured: true };
    return json(route, identity);
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('设置前保留草稿');
  const trigger = page.getByRole('button', { name: '工作区设置', exact: true });
  const nav = page.getByRole('navigation', { name: 'Agent 工作区导航' });
  expect(await trigger.evaluate(element => {
    const button = element.getBoundingClientRect();
    return element.parentElement!.getBoundingClientRect().bottom - button.bottom;
  })).toBeLessThan(20);
  await trigger.click();
  const settings = page.getByRole('region', { name: '工作区设置' });
  const author = settings.getByRole('textbox', { name: 'Git 作者' });
  const email = settings.getByRole('textbox', { name: 'Git 邮箱' });
  await expect(author).toHaveAttribute('readonly', '');
  await expect(author).toHaveValue('');
  releaseRead();
  await expect(author).toBeEnabled();
  await expect(author).toHaveValue('');
  await expect(email).toHaveValue('');
  await expect(settings.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);
  await settings.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(author).toHaveValue('账户姓名');
  await expect(email).toHaveValue('account@example.test');
  await author.fill('工作区作者');
  await email.fill('author@example.test');
  await settings.getByRole('button', { name: '保存', exact: true }).click();
  await expect(settings.getByRole('alert')).toContainText('写入未完成');
  await expect(settings.getByText('已保存并生效')).toHaveCount(0);
  await expect(author).toHaveValue('工作区作者');
  failSave = false;
  await settings.getByRole('button', { name: '保存', exact: true }).click();
  await expect(settings.getByRole('status')).toHaveText('已保存并从目录确认');
  await expect(author).toHaveAttribute('readonly', '');
  expect(identity).toEqual({ author: '工作区作者', email: 'author@example.test', configured: true });
  await page.screenshot({ path: '../../.tmp/fr623-git-settings-desktop.png' });
  await nav.getByRole('button', { name: '主页', exact: true }).click();
  await expect(page).toHaveURL(/scheduled-conversation$/);
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('设置前保留草稿');
  // Reopening must re-read Runtime configuration rather than show stale cache.
  identity = { author: '终端修改后的作者', email: 'changed@example.test', configured: true };
  await trigger.click();
  await expect(author).toHaveValue(identity.author);
  await expect(email).toHaveValue(identity.email);
  identity = { author: '', email: '', configured: false };
  await settings.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(author).toHaveValue('');
  await expect(email).toHaveValue('');
  await expect(settings.getByText('未配置', { exact: true })).toBeVisible();
  await settings.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(author).toHaveValue('账户姓名');
  await expect(email).toHaveValue('account@example.test');
  await settings.getByRole('button', { name: '取消', exact: true }).click();
  await expect(author).toHaveValue('');
  identity = { author: '终端修改后的作者', email: 'changed@example.test', configured: true };
  await settings.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(author).toHaveValue(identity.author);
  await page.setViewportSize({ width: 1024, height: 700 });
  expect(await settings.evaluate(element => element.scrollWidth - element.clientWidth)).toBe(0);
  await expect(settings.getByRole('button', { name: '编辑', exact: true })).toBeInViewport();
  await settings.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(author).toHaveValue(identity.author);
  await author.fill('未保存自定义作者');
  await settings.getByRole('button', { name: '取消', exact: true }).click();
  await expect(author).toHaveValue(identity.author);
  await settings.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(author).toHaveValue('未保存自定义作者');
  await page.screenshot({ path: '../../.tmp/fr623-git-settings-narrow.png' });
});

test('settings toggle returns to the preceding navigation view without changing the route or draft', async ({ page }) => {
  await openWorkspace(page);
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/settings/git', route => json(route, { author: '', email: '', configured: false }));
  await page.goto('/agent/conversations/scheduled-conversation');
  const navigation = page.getByRole('navigation', { name: 'Agent 工作区导航' });
  const trigger = navigation.getByRole('button', { name: '工作区设置', exact: true });
  const settings = page.getByRole('region', { name: '工作区设置' });
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await composer.fill('设置切换保留的草稿');

  const toggleBack = async (view: string) => {
    const route = page.url();
    await trigger.click();
    await expect(settings).toBeVisible();
    await expect(trigger).toHaveAttribute('aria-current', 'page');
    await expect(navigation.getByRole('button', { name: view, exact: true })).not.toHaveAttribute('aria-current', 'page');
    await expect(page).toHaveURL(route);
    await trigger.click();
    await expect(settings).toHaveCount(0);
    await expect(trigger).not.toHaveAttribute('aria-current', 'page');
    await expect(navigation.getByRole('button', { name: view, exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page).toHaveURL(route);
  };

  await toggleBack('主页');
  await expect(composer).toHaveValue('设置切换保留的草稿');
  await toggleBack('主页');
  await navigation.getByRole('button', { name: '定时任务', exact: true }).click();
  await toggleBack('定时任务');
  await expect(page.getByRole('region', { name: '会话定时任务' })).toBeVisible();
  await page.locator('.agent-schedule-tree-select').first().click();
  await page.locator('.agent-schedule-tree-toggle').first().click();
  await page.locator('.agent-schedule-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await expect(composer).toBeVisible();
  await composer.fill('定时会话草稿');
  await toggleBack('定时任务');
  await expect(composer).toHaveValue('定时会话草稿');
  await navigation.getByRole('button', { name: '会话管理', exact: true }).click();
  await toggleBack('会话管理');
  await expect(page.getByRole('heading', { name: '会话管理', exact: true })).toBeVisible();
  // A direct navigation choice while settings is open becomes the next return view.
  await trigger.click();
  await navigation.getByRole('button', { name: '主页', exact: true }).click();
  await toggleBack('主页');
  await expect(composer).toHaveValue('设置切换保留的草稿');
});

test('workspace Git settings keep failed reads empty and allow retry from schedules', async ({ page }) => {
  await openWorkspace(page);
  let fail = true;
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/settings/git', route => fail
    ? json(route, { error: { code: 'UNAVAILABLE', message: '读取失败' } }, 503)
    : json(route, { author: '已有作者', email: 'existing@example.test', configured: true }));
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务', exact: true }).click();
  await page.getByRole('button', { name: '工作区设置', exact: true }).click();
  const settings = page.getByRole('region', { name: '工作区设置' });
  const author = settings.getByRole('textbox', { name: 'Git 作者' });
  await expect(settings.getByRole('alert')).toContainText('无法读取');
  await expect(author).toHaveValue('');
  await expect(author).toHaveAttribute('readonly', '');
  fail = false;
  await settings.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(author).toBeEnabled();
  await expect(author).toHaveValue('已有作者');
});

// Keep native scrollbar thumbs visible so pointer dragging exercises the scrollbar.
test.use({launchOptions:{ignoreDefaultArgs:['--hide-scrollbars']}});
test('conversation list scrollbar appears during scrolling, hides after 1s and stays thin while dragged', async ({ page }) => {
  await page.setViewportSize({width:1440,height:800});
  await openWorkspace(page);
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/work-directories', route => json(route, {
    root:{kind:'ROOT',display_name:'根工作区',working_directory:'/runtime/workspace/project'},
    items:Array.from({length:35},(_,index)=>({id:`scroll-directory-${index}`,display_name:`滚动工作区 ${index}`,current_version:{working_directory:`/runtime/workspace/project/directory-${index}`}})),
  }));
  await page.goto('/agent/conversations/scheduled-conversation');
  const list=page.locator('.agent-workbench-list');
  await expect.poll(()=>list.evaluate(element=>element.scrollHeight>element.clientHeight)).toBe(true);
  const appearance=()=>list.evaluate(element=>{
    const bar=getComputedStyle(element,'::-webkit-scrollbar');
    const track=getComputedStyle(element,'::-webkit-scrollbar-track');
    const thumb=getComputedStyle(element,'::-webkit-scrollbar-thumb');
    return {width:bar.width,gutter:element.getBoundingClientRect().width-element.clientWidth,track:track.backgroundColor,border:track.borderRightWidth,shadow:track.boxShadow,thumb:thumb.backgroundColor};
  });
  await page.mouse.move(800,300);
  await expect.poll(async () => (await appearance()).thumb).toBe('rgba(0, 0, 0, 0)');
  const hidden=await appearance();
  await list.hover({position:{x:40,y:100}});
  expect(await appearance()).toEqual(hidden);
  await page.mouse.wheel(0,120);
  await expect.poll(async () => (await appearance()).thumb).toBe('rgb(165, 176, 168)');
  const before=await appearance();
  expect(parseFloat(before.width)).toBeLessThanOrEqual(8);
  expect(before.track).toBe('rgba(0, 0, 0, 0)');
  expect(before.border).toBe('0px');
  expect(before.shadow).toBe('none');
  const box=(await list.boundingBox())!;
  for(const inset of [1,3,5]){
    expect(await page.evaluate(({x,y})=>Boolean(document.elementFromPoint(x,y)?.closest('.agent-conversation-rail-resizer')),{x:box.x+box.width-inset,y:box.y+20})).toBe(false);
  }
  const rail=page.locator('.agent-workbench-rail');
  const railWidth=await rail.evaluate(element=>element.getBoundingClientRect().width);
  const thumbTop=await list.evaluate(element=>element.scrollTop/element.scrollHeight*element.clientHeight);
  await page.mouse.move(box.x+box.width-2,box.y+thumbTop+20);
  expect(await appearance()).toEqual(before);
  await page.mouse.down();
  const scrollBeforeDrag=await list.evaluate(element=>element.scrollTop);
  await page.mouse.move(box.x+box.width+4,box.y+thumbTop+80,{steps:5});
  expect(await appearance()).toEqual(before);
  await page.mouse.up();
  await expect.poll(()=>rail.evaluate(element=>element.getBoundingClientRect().width)).toBe(railWidth);
  await expect.poll(()=>list.evaluate(element=>element.scrollTop)).toBeGreaterThan(scrollBeforeDrag);
  await list.hover({position:{x:40,y:100}});
  await page.mouse.wheel(0,350);
  await expect.poll(()=>list.evaluate(element=>element.scrollTop)).toBeGreaterThan(0);
  // Verify the exact inactivity delay independently of native scroll animation.
  await expect.poll(async () => (await appearance()).thumb).toBe('rgba(0, 0, 0, 0)');
  await page.clock.install();
  await page.clock.pauseAt(new Date());
  await list.dispatchEvent('scroll');
  await page.clock.runFor(800);
  expect((await appearance()).thumb).toBe('rgb(165, 176, 168)');
  // A new scroll restarts the full 1s delay, even with the pointer on the track.
  await list.dispatchEvent('scroll');
  await page.mouse.move(box.x+box.width-2,box.y+100);
  await page.clock.runFor(999);
  expect((await appearance()).thumb).toBe('rgb(165, 176, 168)');
  await page.clock.runFor(1);
  expect((await appearance()).thumb).toBe('rgba(0, 0, 0, 0)');
  expect(await appearance()).toEqual({...before,thumb:'rgba(0, 0, 0, 0)'});
  await page.clock.resume();
  const handle=page.getByRole('separator',{name:'调整会话列表宽度'});
  const handleBox=(await handle.boundingBox())!;
  await page.mouse.move(handleBox.x+handleBox.width/2,handleBox.y+100);
  await page.mouse.down();
  await page.mouse.move(handleBox.x+handleBox.width/2+48,handleBox.y+100,{steps:5});
  await page.mouse.up();
  await expect.poll(()=>rail.evaluate(element=>element.getBoundingClientRect().width)).toBeGreaterThan(railWidth+30);
});

test('manual transcript scrolling hides the latest-reply button when the final momentum scroll reaches bottom', async ({ page }) => {
  const reply = Array.from({ length: 90 }, (_, index) => `**回复段落 ${index + 1}**：手动滑到最后一段时应该自动隐藏回到底部按钮。`).join('\n\n');
  await openWorkspace(page, [
    { id: 'bottom-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看长回复', timestamp: now } },
    { id: 'bottom-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'bottom-user', content: reply, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const surface = page.locator('.conversation-surface');
  const jump = page.getByRole('button', { name: '跳转到最新回复', exact: true });
  const gap = () => surface.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight);
  await expect(surface.locator('.conversation-message.assistant')).toContainText('回复段落 90');
  await surface.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(gap).toBeLessThanOrEqual(16);
  await surface.hover();
  await page.mouse.wheel(0, -800);
  await expect.poll(gap).toBeGreaterThan(600);
  await expect(jump).toBeVisible();
  const readingGap = await gap();
  await page.mouse.wheel(0, 120);
  await expect.poll(gap).toBeLessThan(readingGap);
  await expect(jump).toBeVisible();
  // A trackpad gesture continues emitting native scroll events after its last
  // wheel input. Advance the remaining scroll frames without another input.
  await surface.evaluate(async element => {
    for (const remaining of [120, 2, 0]) {
      element.scrollTop = element.scrollHeight - element.clientHeight - remaining;
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    }
  });
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(jump).toBeHidden();
  // Ordinary wheel scrolling works again without using the jump button.
  await page.mouse.wheel(0, -400);
  await expect.poll(gap).toBeGreaterThan(300);
  await expect(jump).toBeVisible();
  await page.mouse.wheel(0, 20_000);
  await expect.poll(gap).toBeLessThanOrEqual(2);
  await expect(jump).toBeHidden();
});

for (const scope of ['root', 'directory'] as const) {
  test(`workspace group stays collapsed after route reveal and list refresh (${scope})`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 600 });
    await openWorkspace(page);
    const groups = Object.fromEntries(['root', 'directory'].map(group => [group, Array.from({ length: 6 }, (_, index) => ({
      id: `${group}-reveal-${index}`, display_title: `${group} 会话 ${index}`, title_state: index === 0 ? 'PENDING' : 'MANUAL', lifecycle: 'ACTIVE',
      work_directory_id: group === 'directory' ? 'existing-directory' : null,
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high',
      sort_key: 100 - index, created_at: now, updated_at: now,
    }))]));
    const reads = { root: 0, directory: 0 };
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/conversations') && route.request().method() === 'GET') {
        const group = url.searchParams.get('work_directory_id') === 'existing-directory' ? 'directory' : 'root';
        if (url.searchParams.has('cursor')) return json(route, { items: groups[group].slice(3), next_cursor: null });
        reads[group] += 1;
        const items = groups[group].slice(0, 3).map((item, index) => index === 0
          ? { ...item, updated_at: new Date(Date.parse(now) + reads[group] * 1000).toISOString() }
          : item);
        return json(route, { items, next_cursor: `${group}-later-page` });
      }
      const item = Object.values(groups).flat().find(item => url.pathname.endsWith(`/conversations/${item.id}`));
      if (item && route.request().method() === 'GET') return json(route, item);
      return route.fallback();
    });
    const target = groups[scope][5];
    await page.goto(`/agent/conversations/${target.id}`);
    const groupId = scope === 'root' ? 'root' : 'existing-directory';
    const label = scope === 'root' ? '根工作区' : '已有项目目录';
    const group = page.locator(`[data-workspace-group-id="${groupId}"]`);
    const targetRow = group.locator(`[data-conversation-binding-id="${target.id}"]`);
    await expect(targetRow).toBeInViewport(); // page 2 and beyond the default three rows
    await group.getByRole('button', { name: `收起工作区 ${label}`, exact: true }).click();
    const expand = group.getByRole('button', { name: `展开工作区 ${label}`, exact: true });
    await expect(expand).toHaveAttribute('aria-expanded', 'false');
    await expect(targetRow).not.toBeVisible();
    const readsAfterCollapse = reads[scope];
    await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('手动收起后保留草稿');
    await expect.poll(() => reads[scope]).toBeGreaterThan(readsAfterCollapse + 1);
    await expect(expand).toHaveAttribute('aria-expanded', 'false');
    await expect(targetRow).not.toBeVisible();
    // Switching the sidebar surface must preserve the user's collapsed group.
    await page.getByRole('button', { name: /查看活动会话/ }).click();
    await page.getByRole('button', { name: '返回工作区列表', exact: true }).click();
    await expect(expand).toHaveAttribute('aria-expanded', 'false');
    await expand.click();
    await expect(targetRow).toBeInViewport();
    await expect(targetRow.getByRole('button', { name: target.display_title, exact: true })).toHaveClass(/active/);
    const readsAfterExpand = reads[scope];
    await expect.poll(() => reads[scope]).toBeGreaterThan(readsAfterExpand + 1);
    await expect(targetRow).toBeInViewport();
    await expect(group.getByRole('button', { name: `收起工作区 ${label}`, exact: true })).toBeVisible();
    await group.getByRole('button', { name: `收起工作区 ${label}`, exact: true }).click();
    await expect(expand).toBeVisible();
    // Refresh restores even the active conversation's collapsed group.
    await page.reload();
    await expect(expand).toBeVisible();
    await expect(targetRow).not.toBeVisible();
    await expand.click();
    await expect(targetRow).toBeInViewport();
    await page.reload();
    await expect(targetRow).toBeInViewport();
    await group.getByRole('button', { name: `收起工作区 ${label}`, exact: true }).click();
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversation-activity**', route => json(route, {
      running_binding_ids: [], conversations: [{ ...target, unread: true, unread_origin: 'MANUAL' }],
    }));
    await page.getByRole('button', { name: /查看活动会话/ }).click();
    const activityRow = page.locator('.agent-workspace-activity').locator(`[data-conversation-binding-id="${target.id}"]`);
    await activityRow.dblclick();
    await expect(targetRow).toBeInViewport();
    await expect(group.getByRole('button', { name: `收起工作区 ${label}`, exact: true })).toBeVisible();
  });
}

test('workspace file tree collapses smoothly and ignores a short drag while collapsed', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '文件', exact: true }).click();
  const files = page.locator('.agent-workspace-files');
  const pane = page.locator('.agent-file-tree-pane');
  await expect(pane).toBeVisible();
  const resize = page.getByRole('separator', { name: '调整文件目录宽度', exact: true });
  const box = await resize.boundingBox();
  if (!box) throw new Error('Expected file tree resize handle');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x - 240, box.y + box.height / 2);
  await page.mouse.up();
  await expect(files).toHaveClass(/file-tree-collapsed/);
  await expect(pane).toBeHidden();
  const expand = page.getByRole('separator', { name: '展开文件目录', exact: true });
  const collapsed = await expand.boundingBox();
  if (!collapsed) throw new Error('Expected collapsed file tree resize handle');
  await page.mouse.move(collapsed.x + collapsed.width / 2, collapsed.y + collapsed.height / 2);
  await page.mouse.down();
  await page.mouse.move(collapsed.x + 20, collapsed.y + collapsed.height / 2);
  await page.mouse.up();
  await expect(files).toHaveClass(/file-tree-collapsed/);
  await expand.dblclick();
  await expect(files).not.toHaveClass(/file-tree-collapsed/);
  await expect(pane).toBeVisible();
  await expect.poll(() => pane.evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThanOrEqual(180);
});


test('workspace file tree copies selected names and relative paths with shortcuts', async ({ page, context }) => {
  await openWorkspace(page);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const root = '/runtime/workspace/project';
  await page.route('**/api/v1/agent-workspaces/*/workspace/directory**', route => {
    const parentPath = new URL(route.request().url()).searchParams.get('parent_path') ?? root;
    const entries = parentPath === root
      ? [
        { path: `${root}/src`, kind: 'directory', size: 0 },
        { path: `${root}/README.md`, kind: 'file', size: 128 },
      ]
      : parentPath === `${root}/src`
        ? [{ path: `${root}/src/config.ts`, kind: 'file', size: 42 }]
        : [];
    return json(route, { parent_path: parentPath, entries, next_cursor: null });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '文件', exact: true }).click();
  await page.getByRole('button', { name: 'README.md', exact: true }).click();
  await page.keyboard.press('Meta+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('README.md');
  await page.getByRole('button', { name: 'src', exact: true }).click();
  await expect(page.getByRole('button', { name: 'config.ts', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'config.ts', exact: true }).click();
  await page.keyboard.press('Meta+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('config.ts');
  await page.keyboard.press('Meta+Alt+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('src/config.ts');
});

test('workspace file tree collapses immediately at its minimum expanded width', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '文件', exact: true }).click();
  const files = page.locator('.agent-workspace-files');
  const resize = page.getByRole('separator', { name: '调整文件目录宽度', exact: true });
  const pane = page.locator('.agent-file-tree-pane');
  const box = await resize.boundingBox();
  const initialWidth = await pane.evaluate(element => element.getBoundingClientRect().width);
  if (!box) throw new Error('Expected file tree resize handle');
  const startX = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(startX - (initialWidth - 181), y);
  await page.mouse.up();
  await expect.poll(() => pane.evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThanOrEqual(180);
  await expect(files).not.toHaveClass(/file-tree-collapsed/);
  const minimumWidthBox = await resize.boundingBox();
  if (!minimumWidthBox) throw new Error('Expected visible file tree resize handle');
  const minimumX = minimumWidthBox.x + minimumWidthBox.width / 2;
  const minimumY = minimumWidthBox.y + minimumWidthBox.height / 2;
  await page.mouse.move(minimumX, minimumY);
  await page.mouse.down();
  await page.mouse.move(minimumX - 1, minimumY);
  await page.mouse.up();
  await expect(files).toHaveClass(/file-tree-collapsed/);
});

test('clicking a conversation keeps the sidebar list at its current position', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await openWorkspace(page, [], true);
  const conversations = Array.from({ length: 24 }, (_, index) => ({
    id: `scroll-stable-${index}`,
    display_title: `稳定会话 ${index}`,
    title_state: 'MANUAL',
    lifecycle: 'ACTIVE',
    streaming_callback_ready: true,
    write_available: true,
    execution_status: 'idle',
    created_at: now,
    updated_at: now,
  }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations?**', route => json(route, { items: conversations, next_cursor: null }));
  await page.goto('/agent/conversations/scroll-stable-0');
  const sidebar = page.locator('.agent-workbench-list');
  await expect.poll(() => sidebar.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  await sidebar.hover();
  await page.mouse.wheel(0, 260);
  await expect.poll(() => sidebar.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  const scrollTop = await sidebar.evaluate(element => element.scrollTop);
  const visibleBindingId = await sidebar.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    return [...element.querySelectorAll<HTMLElement>('[data-conversation-binding-id]')]
      .find(row => row.dataset.conversationBindingId !== 'scroll-stable-0'
        && row.getBoundingClientRect().top >= bounds.top
        && row.getBoundingClientRect().bottom <= bounds.bottom)
      ?.dataset.conversationBindingId;
  });
  expect(visibleBindingId).toBeTruthy();
  await page.locator(`[data-conversation-binding-id="${visibleBindingId}"]`).getByRole('button').click();
  await expect(page).toHaveURL(new RegExp(`/agent/conversations/${visibleBindingId}$`));
  await settleWorkspaceLayout(page);
  expect(await sidebar.evaluate(element => element.scrollTop)).toBe(scrollTop);
});


test('workspace and pinned collapse preferences survive reopening with independent scopes', async ({ page }) => {
  await openWorkspace(page, [], true);
  const preferenceKey = 'flowweave:sidebar-collapse:v1:schedule-user:agent-workspace:schedule-workspace';
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/pinned-conversations', route => json(route, [{
    id: 'pinned-conversation', display_title: '置顶的测试会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', pinned: true,
    execution_status: 'idle', streaming_callback_ready: true, write_available: true, created_at: now, updated_at: now,
  }]));
  await page.reload();
  await page.getByRole('button', { name: '收起工作区 根工作区', exact: true }).click();
  await page.getByRole('button', { name: '收起工作区 已有项目目录', exact: true }).click();
  await page.getByRole('button', { name: '收起置顶会话', exact: true }).click();
  await page.reload();
  for (const label of ['根工作区', '已有项目目录']) {
    await expect(page.getByRole('button', { name: `展开工作区 ${label}`, exact: true })).toHaveAttribute('aria-expanded', 'false');
  }
  await expect(page.getByRole('button', { name: '展开置顶会话', exact: true })).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await expect(page.getByRole('button', { name: '展开工作区 根工作区', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '展开工作区 根工作区', exact: true }).click();
  await expect(page.getByRole('button', { name: '展开工作区 已有项目目录', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '展开置顶会话', exact: true })).toBeVisible();
  // A different workspace must neither read nor overwrite the saved state.
  await page.route('**/api/v1/agent-workspaces/default', route => json(route, { id: 'other-workspace', display_name: '其他工作区', desired_state: 'RUNNING', updated_at: now }));
  await page.reload();
  await expect(page.getByRole('button', { name: '收起工作区 已有项目目录', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起工作区 根工作区', exact: true }).click();
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), preferenceKey)).toEqual({ groups: ['existing-directory'], pinned: true });
});


const pinnedOrderPreferenceKey = 'flowweave:pinned-conversation-order:v1:schedule-user:agent-workspace:schedule-workspace';

async function mockPinnedSidebar(page: Page) {
  await openWorkspace(page);
  const items = Array.from({ length: 3 }, (_, index) => ({
    id: `pinned-${index}`, display_title: `置顶会话 ${index}`, title_state: 'MANUAL', lifecycle: 'ACTIVE', pinned: true,
    work_directory_id: index === 1 ? 'existing-directory' : null, sort_key: 100 - index,
    execution_status: 'idle', streaming_callback_ready: true, write_available: true, created_at: now, updated_at: now,
  }));
  const orderWrites: unknown[] = [];
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/pinned-conversations', route => json(route, items.filter(item => item.pinned)));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations/*/order', route => {
    orderWrites.push(route.request().postDataJSON());
    return json(route, items[0]);
  });
  await page.reload();
  await expect(page.getByRole('region', { name: '置顶会话', exact: true })).toBeVisible();
  return { items, orderWrites };
}

test('pinned conversations reorder across workspaces and restore their independent browser preference', async ({ page }) => {
  const { orderWrites } = await mockPinnedSidebar(page);
  const pinned = page.getByRole('region', { name: '置顶会话', exact: true });
  const titles = () => pinned.locator('.agent-workspace-conversation-select b').allTextContents();
  const source = pinned.getByRole('button', { name: '置顶会话 1', exact: true });
  await expect(source).toHaveAttribute('draggable', 'true');
  const target = pinned.getByRole('button', { name: '置顶会话 0', exact: true });
  await source.dragTo(target, { targetPosition: { x: 20, y: 2 } });
  await expect.poll(titles).toEqual(['置顶会话 1', '置顶会话 0', '置顶会话 2']);
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), pinnedOrderPreferenceKey)).toEqual(['pinned-1', 'pinned-0', 'pinned-2']);
  expect(orderWrites).toEqual([]); // Original workspace ordering is untouched.
  await page.reload();
  await expect.poll(titles).toEqual(['置顶会话 1', '置顶会话 0', '置顶会话 2']);
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await expect.poll(titles).toEqual(['置顶会话 1', '置顶会话 0', '置顶会话 2']);
  await pinned.getByRole('button', { name: '置顶会话 1', exact: true }).dragTo(page.locator('.agent-workbench-content'));
  await expect(page.getByRole('button', { name: '移除会话 置顶会话 1', exact: true })).toBeVisible();
  await expect.poll(titles).toEqual(['置顶会话 1', '置顶会话 0', '置顶会话 2']);
  // A different identity scope must neither read nor overwrite this preference.
  await page.route('**/api/v1/auth/me', route => json(route, { id: 'other-user', username: 'other-user', role: 'USER', can_use_agent_sessions: true }));
  await page.reload();
  await expect.poll(titles).toEqual(['置顶会话 0', '置顶会话 1', '置顶会话 2']);
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), pinnedOrderPreferenceKey)).toEqual(['pinned-1', 'pinned-0', 'pinned-2']);
});

for (const width of [1440, 1024]) {
  test(`conversation page uses one drop overlay across transcript and composer at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await openWorkspace(page);
    await page.goto('/agent/conversations/scheduled-conversation');
    const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
    await expect(input).toBeEnabled();
    await input.fill('拖放时保留草稿');
    const main = page.locator('.agent-workbench-main');
    const content = main.locator('.agent-workbench-content');
    const overlay = main.locator('.agent-workbench-drop-overlay');
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData('application/x-flowweave-conversation-reference', JSON.stringify({ bindingId: 'reference-source', eventId: 'source-event', title: '拖入的会话' }));
      return data;
    });
    await content.dispatchEvent('dragenter', { dataTransfer: transfer });
    await expect(overlay).toBeVisible();
    const bounds = await main.boundingBox();
    expect(await overlay.boundingBox()).toEqual(bounds);
    const inputBounds = await input.boundingBox();
    expect(inputBounds!.y + inputBounds!.height).toBeLessThanOrEqual(bounds!.y + bounds!.height);
    // Moving between children must keep the same page overlay without a local input target.
    await content.dispatchEvent('dragleave', { dataTransfer: transfer, relatedTarget: await input.elementHandle() });
    await input.dispatchEvent('dragenter', { dataTransfer: transfer });
    await input.dispatchEvent('dragover', { dataTransfer: transfer });
    await expect(overlay).toBeVisible();
    await expect(main.locator('.agent-composer-file-drop')).toHaveCount(0);
    await page.screenshot({ path: `../../.tmp/fr649-page-drop-${width}.png` });
    await input.dispatchEvent('drop', { dataTransfer: transfer });
    await expect(overlay).toHaveCount(0);
    await expect(main.getByRole('button', { name: '移除会话 拖入的会话', exact: true })).toHaveCount(1);
    await expect(input).toHaveValue('拖放时保留草稿');

    const files = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData('application/x-flowweave-workspace-file-path', '/runtime/workspace/project/src/main.ts');
      return data;
    });
    await main.locator('.agent-workbench-header').dispatchEvent('dragenter', { dataTransfer: files });
    await expect(overlay).toBeVisible();
    await main.locator('.agent-workbench-header').dispatchEvent('drop', { dataTransfer: files });
    const composerReferences = main.getByLabel('已添加的附件、会话引用和工作区引用');
    await expect(composerReferences).toContainText('main.ts');
    await expect(main.getByLabel('已添加的工作区引用')).toHaveCount(0);
    await expect(composerReferences.getByText('工作区文件 · 本地路径引用')).toHaveCount(0);
    const localReference = composerReferences.getByTitle('预览本地引用：main.ts', { exact: true }).locator('..');
    await expect(localReference).toHaveCSS('max-width', '230px');
    await expect(localReference).toHaveCSS('white-space', 'nowrap');
    await composerReferences.getByRole('button', { name: '引用本地文件 1', exact: true }).click();
    await expect(input).toHaveValue('拖放时保留草稿@本地文件1');
    await composerReferences.getByRole('button', { name: '移除工作区引用 main.ts', exact: true }).click();
    await expect(composerReferences.getByRole('button', { name: '移除工作区引用 main.ts', exact: true })).toHaveCount(0);
    await expect(main.getByRole('button', { name: '移除附件 main.ts', exact: true })).toHaveCount(0);
    await input.dispatchEvent('dragenter', { dataTransfer: transfer });
    await main.dispatchEvent('dragleave', { dataTransfer: transfer });
    await expect(overlay).toHaveCount(0);
    await input.dispatchEvent('dragenter', { dataTransfer: transfer });
    await page.evaluate(() => window.dispatchEvent(new Event('dragend')));
    await expect(overlay).toHaveCount(0);
    await input.dispatchEvent('dragenter', { dataTransfer: transfer });
    await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    await expect(overlay).toHaveCount(0);
    const text = await page.evaluateHandle(() => { const data = new DataTransfer(); data.setData('text/plain', '普通文本'); return data; });
    await input.dispatchEvent('dragenter', { dataTransfer: text });
    await expect(overlay).toHaveCount(0);
  });
}

test('files dropped on the composer upload once through the conversation page', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
  await expect(input).toBeEnabled();
  const creates: unknown[] = [];
  const completions: string[] = [];
  await page.route('**/attachments/uploads**', route => {
    creates.push(route.request().postDataJSON());
    return json(route, { upload_id: 'page-drop', uploaded_parts: [], chunk_size: 1024 });
  });
  await page.route('**/attachments/uploads/page-drop/**', route => {
    if (new URL(route.request().url()).pathname.endsWith('/complete')) {
      completions.push(route.request().url());
      return json(route, { filename: 'page-drop.txt', mime_type: 'text/plain', byte_size: 4, path: '/runtime/workspace/project/uploads/page-drop.txt' });
    }
    return json(route, {});
  });
  const transfer = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(new File(['test'], 'page-drop.txt', { type: 'text/plain' }));
    return data;
  });
  await input.dispatchEvent('dragenter', { dataTransfer: transfer });
  await expect(page.locator('.agent-workbench-drop-overlay')).toBeVisible();
  await input.dispatchEvent('drop', { dataTransfer: transfer });
  await expect(page.locator('.agent-workbench-drop-overlay')).toHaveCount(0);
  await expect(page.locator('.agent-composer').getByTitle('预览附件：page-drop.txt', { exact: true })).toBeVisible();
  expect(creates).toHaveLength(1);
  expect(completions).toHaveLength(1);
});

test('pinned drag rejects ordinary workspace rows while ordinary ordering still persists', async ({ page }) => {
  const { items, orderWrites } = await mockPinnedSidebar(page);
  const ordinary = ['a', 'b'].map((id, index) => ({ ...items[0], id: `ordinary-${id}`, display_title: `普通会话 ${id}`, pinned: false, sort_key: 20 - index }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations?*', route => json(route, { items: ordinary, next_cursor: null }));
  await page.reload();
  const pinned = page.getByRole('region', { name: '置顶会话', exact: true });
  const root = page.locator('[data-workspace-group-id="root"]');
  await pinned.getByRole('button', { name: '置顶会话 0', exact: true }).dragTo(root.getByRole('button', { name: '普通会话 a', exact: true }), { targetPosition: { x: 20, y: 2 } });
  expect(orderWrites).toEqual([]);
  await expect(pinned.locator('.agent-workspace-conversation-select b')).toHaveText(['置顶会话 0', '置顶会话 1', '置顶会话 2']);
  await root.getByRole('button', { name: '普通会话 b', exact: true }).dragTo(root.getByRole('button', { name: '普通会话 a', exact: true }), { targetPosition: { x: 20, y: 2 } });
  await expect(root.locator('.agent-workspace-conversation-select b')).toHaveText(['普通会话 b', '普通会话 a']);
  await expect.poll(() => orderWrites).toEqual([{ ordered_binding_ids: ['ordinary-b', 'ordinary-a'] }]);
  await expect(pinned.locator('.agent-workspace-conversation-select b')).toHaveText(['置顶会话 0', '置顶会话 1', '置顶会话 2']);
});

test('pinned order tolerates stale duplicate ids, corrupt storage and unavailable persistence', async ({ page }) => {
  await mockPinnedSidebar(page);
  const pinned = page.getByRole('region', { name: '置顶会话', exact: true });
  const titles = () => pinned.locator('.agent-workspace-conversation-select b').allTextContents();
  await page.evaluate(key => localStorage.setItem(key, JSON.stringify(['missing', 'pinned-2', 'pinned-2', 42])), pinnedOrderPreferenceKey);
  await page.reload();
  await expect.poll(titles).toEqual(['置顶会话 2', '置顶会话 0', '置顶会话 1']);
  await page.evaluate(key => localStorage.setItem(key, '{invalid'), pinnedOrderPreferenceKey);
  await page.reload();
  await expect.poll(titles).toEqual(['置顶会话 0', '置顶会话 1', '置顶会话 2']);
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith('flowweave:pinned-conversation-order:')) throw new DOMException('Blocked', 'SecurityError');
      return original.call(this, key, value);
    };
  });
  await pinned.getByRole('button', { name: '置顶会话 2', exact: true }).dragTo(pinned.getByRole('button', { name: '置顶会话 0', exact: true }), { targetPosition: { x: 20, y: 2 } });
  await expect.poll(titles).toEqual(['置顶会话 2', '置顶会话 0', '置顶会话 1']);
});

test('a late unpin response preserves navigation and manually collapsed groups', async ({ page }) => {
  const { items } = await mockPinnedSidebar(page);
  const target = items[0];
  let release: () => void = () => undefined;
  const responseGate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/v1/agent-workspaces/schedule-workspace/conversations/${target.id}/pinned`, async route => {
    await responseGate;
    target.pinned = false;
    return json(route, target);
  });
  const pinned = page.getByRole('region', { name: '置顶会话', exact: true });
  const row = pinned.locator(`[data-conversation-binding-id="${target.id}"]`);
  await row.click({ button: 'right' });
  await page.getByRole('menuitem', { name: '取消置顶', exact: true }).click();
  await expect(row).toHaveCount(0);
  const root = page.locator('[data-workspace-group-id="root"]');
  await root.getByRole('button', { name: '日报会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
  await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('切换后的草稿');
  await root.getByRole('button', { name: '收起工作区 根工作区', exact: true }).click();
  release();
  await expect.poll(() => target.pinned).toBe(false);
  await expect(root.getByRole('button', { name: '展开工作区 根工作区', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('切换后的草稿');
  await expect(row).toHaveCount(0);
});

for (const scope of ['root', 'directory', 'active'] as const) {
  test(`unpin reveals the original collapsed workspace through multiple pages (${scope})`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 600 });
    const { items } = await mockPinnedSidebar(page);
    const target = items[scope === 'directory' ? 1 : 0];
    const groupId = target.work_directory_id ?? 'root';
    const groupLabel = scope === 'directory' ? '已有项目目录' : '根工作区';
    const originalGroup = Array.from({ length: 8 }, (_, index) => ({ ...target, id: `original-${index}`, display_title: `原会话 ${index}`, pinned: false, sort_key: 200 - index }));
    const all = [...originalGroup, target];
    const pageReads: string[] = [];
    let failUnpin = true;
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations**', route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith(`/conversations/${target.id}/pinned`)) {
        if (failUnpin) return json(route, { error: { code: 'SAVE_FAILED', message: '暂时无法保存' } }, 503);
        target.pinned = false;
        return json(route, target);
      }
      if (url.pathname.endsWith(`/conversations/${target.id}`)) return json(route, target);
      if (!url.pathname.endsWith('/conversations')) return route.fallback();
      const sameScope = scope === 'directory' ? url.searchParams.get('work_directory_id') === groupId : !url.searchParams.has('work_directory_id');
      if (!sameScope) return json(route, { items: [], next_cursor: null });
      const offset = Number(url.searchParams.get('cursor') ?? 0);
      if (offset) pageReads.push(String(offset));
      return json(route, { items: all.slice(offset, offset + 3), next_cursor: offset + 3 < all.length ? String(offset + 3) : null });
    });
    await page.goto(`/agent/conversations/${scope === 'active' ? target.id : scope === 'root' ? 'original-0' : 'scheduled-conversation'}`);
    await page.getByRole('textbox', { name: '发送 Agent 消息' }).fill('保留当前草稿');
    const group = page.locator(`[data-workspace-group-id="${groupId}"]`);
    await group.getByRole('button', { name: `收起工作区 ${groupLabel}`, exact: true }).click();
    const pinned = page.getByRole('region', { name: '置顶会话', exact: true });
    const pinnedRow = pinned.locator(`[data-conversation-binding-id="${target.id}"]`);
    await pinnedRow.click({ button: 'right' });
    await page.getByRole('menuitem', { name: '取消置顶', exact: true }).click();
    await expect(pinnedRow).toBeVisible(); // Failed persistence restores the pin.
    await expect(group.getByRole('button', { name: `展开工作区 ${groupLabel}`, exact: true })).toBeVisible();
    expect(pageReads).toEqual([]);
    failUnpin = false;
    await pinnedRow.click({ button: 'right' });
    await page.getByRole('menuitem', { name: '取消置顶', exact: true }).click();
    const restoredRow = group.locator(`[data-conversation-binding-id="${target.id}"]`);
    await expect(restoredRow).toBeInViewport();
    if (scope === 'directory') await page.screenshot({ path: test.info().outputPath('unpin-revealed.png') });
    await expect(pinnedRow).toHaveCount(0);
    await expect(group.getByRole('button', { name: `收起工作区 ${groupLabel}`, exact: true })).toBeVisible();
    expect(pageReads).toEqual(['3', '6']);
    await expect(page).toHaveURL(new RegExp(`/agent/conversations/${scope === 'active' ? target.id : scope === 'root' ? 'original-0' : 'scheduled-conversation'}$`));
    await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('保留当前草稿');
    await expect(restoredRow.getByRole('button', { name: target.display_title, exact: true })).toHaveAttribute('draggable', 'true');
    await page.reload();
    await expect(pinnedRow).toHaveCount(0);
  });
}

test('collapsing a workspace near the viewport top preserves its header anchor', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openWorkspace(page, [], true);
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/work-directories', route => json(route, {
    root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' },
    items: Array.from({ length: 6 }, (_, index) => ({ id: `anchor-directory-${index}`, display_name: `锚点工作区 ${index}`, current_version: { working_directory: `/runtime/workspace/project/anchor-${index}` } })),
  }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations?**', async route => {
    const directory = new URL(route.request().url()).searchParams.get('work_directory_id');
    return json(route, { items: Array.from({ length: 3 }, (_, index) => ({
      id: `${directory ?? 'root'}-anchor-${index}`, display_title: `${directory ?? 'root'} 会话 ${index}`, title_state: 'MANUAL', lifecycle: 'ACTIVE',
      work_directory_id: directory, streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now,
    })), next_cursor: null });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  const sidebar = page.locator('.agent-workbench-list');
  const group = page.locator('[data-workspace-group-id="anchor-directory-2"]');
  const header = group.locator(':scope > header');
  await expect(group.locator('[data-conversation-binding-id]')).toHaveCount(3);
  await sidebar.evaluate(element => {
    const group = element.querySelector('[data-workspace-group-id="anchor-directory-2"]')!;
    element.scrollTop += group.getBoundingClientRect().top - element.getBoundingClientRect().top - 12;
  });
  await settleWorkspaceLayout(page);
  const top = await header.evaluate(element => element.getBoundingClientRect().top);
  const toggleBounds = await group.getByRole('button', { name: '收起工作区 锚点工作区 2', exact: true }).boundingBox();
  await page.mouse.click(toggleBounds!.x + 30, toggleBounds!.y + toggleBounds!.height / 2);
  await expect(group.getByRole('button', { name: '展开工作区 锚点工作区 2', exact: true })).toBeVisible();
  await settleWorkspaceLayout(page);
  await expect.poll(() => header.evaluate(element => element.getBoundingClientRect().top)).toBe(top);
  // Keep sampling after native scroll anchoring and subsequent renders settle.
  expect(await sidebar.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  const tops = await header.evaluate(async element => {
    const values: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      values.push(element.getBoundingClientRect().top);
    }
    return values;
  });
  expect(tops.every(value => Math.abs(value - top) <= 1)).toBe(true);
  await group.getByRole('button', { name: '展开工作区 锚点工作区 2', exact: true }).click();
  await settleWorkspaceLayout(page);
  await expect.poll(() => header.evaluate(element => element.getBoundingClientRect().top)).toBe(top);
  const expandedTops = await header.evaluate(async element => {
    const values: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      values.push(element.getBoundingClientRect().top);
    }
    return values;
  });
  expect(expandedTops.every(value => Math.abs(value - top) <= 1)).toBe(true);
  // Repeated collapse near the old scroll limit must not accumulate blank tail.
  await group.getByRole('button', { name: '收起工作区 锚点工作区 2', exact: true }).click();
  await expect.poll(() => header.evaluate(element => element.getBoundingClientRect().top)).toBe(top);
  await sidebar.hover();
  await page.mouse.wheel(0, -2000);
  await expect.poll(() => sidebar.evaluate(element => element.scrollTop)).toBe(0);
  expect(await sidebar.locator(':scope > [aria-hidden="true"]').evaluate(element => element.getBoundingClientRect().height)).toBe(0);
});

test('workspace directory search browses one level at a time independently of checkbox selection', async ({ page }) => {
  await openWorkspace(page);
  const root = '/runtime/workspace/project';
  const paths = [
    'adjust', 'adjust/aaa', 'adjust/aaa/deep', 'adjust/bbb', 'adjust/bbb/aaa-hidden',
    'batch-adjust', 'batch-adjust/aaa-other', 'backend', 'backend/aaa',
    'frontend', 'frontend/src', 'frontend/src/nested',
    ...Array.from({ length: 35 }, (_, index) => `project-${index}`),
  ];
  const directoryReads: string[] = [];
  let fullIndexRequests = 0;
  page.on('request', request => {
    if (new URL(request.url()).searchParams.get('full_index') === 'true') fullIndexRequests += 1;
  });
  await page.route('**/api/v1/agent-workspaces/*/workspace/directory**', async route => {
    const url = new URL(route.request().url());
    const parent = url.searchParams.get('parent_path') ?? root;
    directoryReads.push(parent);
    const entries = paths.map(path => ({ path: `${root}/${path}`, kind: 'directory', size: 0 }))
      .filter(entry => entry.path.slice(0, entry.path.lastIndexOf('/')) === parent);
    const offset = Number(url.searchParams.get('cursor') ?? 0);
    const pageSize = 20;
    await json(route, { parent_path: parent, entries: entries.slice(offset, offset + pageSize), next_cursor: offset + pageSize < entries.length ? String(offset + pageSize) : null });
  });
  await page.getByRole('button', { name: '新增工作区', exact: true }).click();
  const creator = page.getByRole('dialog', { name: '新增工作区' });
  const search = creator.getByRole('searchbox', { name: '搜索目录' });
  await expect(creator.getByRole('checkbox', { name: 'backend', exact: true })).toBeVisible();
  for(const viewport of [{width:1440,height:1000},{width:800,height:700},{width:390,height:760}]){
    await page.setViewportSize(viewport);
    const geometry=await creator.evaluate(element=>{
      const dialog=element.getBoundingClientRect();
      const picker=element.querySelector('.agent-work-directory-picker')!;
      const header=picker.querySelector('header')!.getBoundingClientRect();
      const list=picker.querySelector(':scope > div')!;
      const name=element.querySelector('.agent-work-directory-name')!.getBoundingClientRect();
      return {ratio:list.getBoundingClientRect().height/dialog.height,toolbar:header.height,name:name.height,overflow:element.scrollWidth-element.clientWidth,listScrollable:list.scrollHeight>list.clientHeight};
    });
    expect(geometry.ratio).toBeGreaterThan(.55);
    expect(geometry.toolbar).toBeLessThanOrEqual(44);
    expect(geometry.name).toBeLessThanOrEqual(56);
    expect(geometry.overflow).toBe(0);
    expect(geometry.listScrollable).toBe(true);
    await expect(creator.getByRole('button',{name:'创建工作区',exact:true})).toBeInViewport();
    await creator.screenshot({path:test.info().outputPath(`create-workspace-${viewport.width}.png`)});
  }
  const checkbox = (name: string) => creator.getByRole('checkbox', { name, exact: true });
  const browse = (name: string) => creator.getByRole('button', { name: `浏览目录 ${name}`, exact: true });
  await expect(checkbox('adjust')).toBeVisible();
  await expect(checkbox('adjust/aaa')).toHaveCount(0);
  await expect(checkbox('frontend/src')).toHaveCount(0);
  expect(new Set(directoryReads)).toEqual(new Set([root]));
  expect(directoryReads.filter(path => path === root)).toHaveLength(2); // direct-level pagination
  expect(fullIndexRequests).toBe(0);

  await search.fill('adjust');
  await expect(checkbox('adjust')).toBeVisible();
  await expect(checkbox('batch-adjust')).toBeVisible();
  await expect(checkbox('adjust/aaa')).toHaveCount(0);
  await expect(creator.getByRole('treeitem', { expanded: true })).toHaveCount(0);
  expect(new Set(directoryReads)).toEqual(new Set([root]));
  await checkbox('adjust').check();
  await expect(search).toHaveValue('adjust');
  await expect(checkbox('adjust/aaa')).toHaveCount(0);
  await expect(creator.getByLabel('工作区名称')).toHaveValue('adjust');
  await checkbox('adjust').uncheck();
  await expect(search).toHaveValue('adjust');

  await browse('adjust').click();
  await expect(search).toHaveValue('adjust/');
  await expect(checkbox('adjust/aaa')).toBeVisible();
  await expect(checkbox('adjust/bbb')).toBeVisible();
  await expect(checkbox('adjust/aaa/deep')).toHaveCount(0);
  await expect(checkbox('adjust')).not.toBeChecked();
  await expect(checkbox('batch-adjust')).toHaveCount(0);
  await browse('adjust').click();
  await expect(search).toHaveValue('');
  await expect(checkbox('adjust/aaa')).toHaveCount(0);
  await expect(checkbox('backend')).toBeVisible();

  await search.fill('adjust/aaa');
  await expect(checkbox('adjust/aaa')).toBeVisible();
  await expect(checkbox('adjust/bbb')).toHaveCount(0);
  await expect(checkbox('backend/aaa')).toHaveCount(0);
  await expect(checkbox('adjust/aaa/deep')).toHaveCount(0);
  await expect(checkbox('adjust/bbb/aaa-hidden')).toHaveCount(0);
  expect(new Set(directoryReads)).toEqual(new Set([root, `${root}/adjust`]));
  await browse('adjust/aaa').click();
  await expect(search).toHaveValue('adjust/aaa/');
  await expect(checkbox('adjust/aaa/deep')).toBeVisible();
  await creator.getByRole('button', { name: '收起目录 adjust/aaa', exact: true }).click();
  await expect(search).toHaveValue('adjust/');
  await expect(checkbox('adjust/aaa/deep')).toHaveCount(0);
  await creator.getByRole('button', { name: '收起目录 adjust', exact: true }).click();
  await expect(search).toHaveValue('');

  await search.fill('aaa');
  await expect(creator.getByText('没有匹配的目录。')).toBeVisible();
  await search.fill('project-34');
  await expect(checkbox('project-34')).toBeVisible(); // matches from later root pages
  await search.fill('frontend/src');
  await expect(checkbox('frontend/src')).toBeVisible();
  await expect(checkbox('frontend/src/nested')).toHaveCount(0);
  await checkbox('frontend/src').check();
  await expect(search).toHaveValue('frontend/src');
  await expect(creator.getByLabel('已选择 1 个目录，最多 20 个')).toHaveText('1/20');
  await expect(creator.getByRole('button', { name: '创建工作区', exact: true })).toBeEnabled();
  await browse('frontend/src').click();
  await expect(search).toHaveValue('frontend/src/');
  await expect(checkbox('frontend/src/nested')).toBeVisible();
  await creator.screenshot({ path: test.info().outputPath('directory-path-browsing.png') });
  await browse('frontend/src/nested').click();
  await expect(search).toHaveValue('frontend/src/nested/');
  await expect(creator.getByText('此目录没有子目录。')).toBeVisible();
  await browse('frontend/src/nested').click();
  await expect(search).toHaveValue('frontend/src/');
  await checkbox('frontend/src').uncheck();
  await expect(search).toHaveValue('frontend/src/');
  await browse('frontend/src').click();
  await expect(search).toHaveValue('frontend/');
  await search.fill('');
  await expect(checkbox('backend')).toBeVisible();
  await expect(checkbox('frontend/src')).toHaveCount(0);
  expect(fullIndexRequests).toBe(0);
  expect(directoryReads).not.toContain(`${root}/backend`);
  expect(directoryReads).not.toContain(`${root}/batch-adjust`);
  expect(directoryReads).not.toContain(`${root}/adjust/bbb`);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await creator.getByRole('button', { name: '关闭新增工作区' }).click();
  await page.getByRole('button', { name: '新增工作区', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(checkbox('frontend/src')).toHaveCount(0);
  await expect(creator.getByRole('treeitem', { expanded: true })).toHaveCount(0);
});

test('selecting a task after opening its run returns to that task detail', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await tasks.locator('.agent-schedule-tree-select').first().click();
  await tasks.locator('.agent-schedule-tree-toggle').first().click();
  await tasks.locator('.agent-schedule-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toBeVisible();

  await tasks.locator('.agent-schedule-tree-select').filter({ hasText: '每周复核' }).click();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
  await expect(page.getByRole('heading', { name: '每周复核', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveCount(0);
});

test('task selection and arrow expansion are independent without action menus', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/schedules');
  const tasks = page.getByRole('region', { name: '会话定时任务' });
  const first = tasks.locator('.agent-schedule-tree-group').first();
  const second = tasks.locator('.agent-schedule-tree-group').nth(1);
  const select = first.locator('.agent-schedule-tree-select');
  const arrow = first.locator('.agent-schedule-tree-toggle');
  const detail = tasks.locator('.agent-schedule-detail');
  await expect(arrow).toHaveAttribute('aria-expanded', 'false');
  await expect(tasks.locator('.agent-schedule-menu')).toHaveCount(0);
  // Clicking the row, including its title and status, only opens details.
  await select.locator('b').click();
  await expect(detail.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(select).toHaveAttribute('aria-current', 'true');
  await select.locator('small').click();
  await expect(arrow).toHaveAttribute('aria-expanded', 'false');
  await expect(first.locator('.agent-schedule-run')).toHaveCount(0);
  await arrow.click();
  await expect(arrow).toHaveAttribute('aria-expanded', 'true');
  await expect(first.locator('.agent-schedule-run')).toHaveCount(3);
  await arrow.click();
  await expect(arrow).toHaveAttribute('aria-expanded', 'false');
  await expect(detail.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  // The other task's arrow changes only that task's expansion, not selection.
  await second.locator('.agent-schedule-tree-toggle').click();
  await expect(second.locator('.agent-schedule-run')).toHaveCount(3);
  await expect(second.locator('.agent-schedule-tree-select')).not.toHaveAttribute('aria-current', 'true');
  await expect(detail.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await second.locator('.agent-schedule-tree-select').click();
  await expect(detail.getByRole('heading', { name: '每周复核', exact: true })).toBeVisible();
  await expect(arrow).toHaveAttribute('aria-expanded', 'false');
  await expect(second.locator('.agent-schedule-run')).toHaveCount(3);
  await arrow.focus();
  await arrow.press('Enter');
  await expect(first.locator('.agent-schedule-run')).toHaveCount(3);
  await arrow.press('Space');
  await expect(first.locator('.agent-schedule-run')).toHaveCount(0);
  await select.focus();
  await select.press('Enter');
  await expect(detail.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(arrow).toHaveAttribute('aria-expanded', 'false');
  await detail.getByRole('button', { name: '编辑', exact: true }).click();
  await expect(page.getByLabel('任务名称', { exact: true })).toHaveValue('每日项目检查');
});

test('schedule arrows expand independently and stay collapsed during active-session polling', async ({ page }) => {
  let reads = 0;
  page.on('request', request => { if (new URL(request.url()).pathname.endsWith('/conversation-schedules')) reads += 1; });
  await openWorkspace(page);
  await page.goto('/agent/schedules');
  const tasks = page.getByRole('region', { name: '会话定时任务' });
  const first = tasks.locator('.agent-schedule-tree-group').first();
  const second = tasks.locator('.agent-schedule-tree-group').nth(1);
  await first.locator('.agent-schedule-tree-select').click();
  await first.locator('.agent-schedule-tree-toggle').click();
  await second.locator('.agent-schedule-tree-toggle').click();
  await expect(tasks.locator('.agent-schedule-run')).toHaveCount(6);
  await first.locator('.agent-schedule-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await expect(second.locator('.agent-schedule-tree-toggle')).toHaveAttribute('aria-expanded', 'true');
  await first.locator('.agent-schedule-tree-toggle').click();
  await expect(first.locator('.agent-schedule-run')).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  const initialReads = reads;
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  await expect(first.locator('.agent-schedule-tree-toggle')).toHaveAttribute('aria-expanded', 'false');
  // Selecting an already collapsed task returns from a run to its detail.
  await first.locator('.agent-schedule-tree-select').click();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
  await expect(tasks.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(first.locator('.agent-schedule-run')).toHaveCount(0);
  await expect(second.locator('.agent-schedule-run')).toHaveCount(3);
  await tasks.screenshot({ path: test.info().outputPath('schedule-selection.png') });
});

test('schedule detail keeps long prompts and run scrolling inside a single screen', async ({ page }) => {
  const prompt = Array.from({ length: 30 }, (_, index) => `第 ${index + 1} 段业务要求：检查项目状态，汇总运行结果并生成完整报告。`).join('\n\n');
  await openWorkspace(page, [], false, '日报会话', { prompt, occurrenceCount: 40 });
  await page.goto('/agent/schedules');
  await page.locator('.agent-schedule-tree-select').first().click();
  const detail = page.locator('.agent-schedule-detail');
  const list = detail.getByRole('region', { name: '运行会话列表' });
  await expect(list.locator('.agent-schedule-history-run')).toHaveCount(20);
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 1440, height: 600 }, { width: 1024, height: 700 }]) {
    await page.setViewportSize(viewport);
    await expect(detail.getByRole('button', { name: '查看全文' })).toBeVisible();
    expect(await list.locator('.agent-schedule-history-run').count()).toBeGreaterThanOrEqual(20);
    await expect.poll(() => page.evaluate(() => {
      const main = document.querySelector<HTMLElement>('.agent-schedule-detail-main')!;
      const list = document.querySelector<HTMLElement>('.agent-schedule-history-list')!;
      const rect = list.getBoundingClientRect();
      return { mainOverflowY: Math.max(0, main.scrollHeight - main.clientHeight), mainOverflowX: Math.max(0, main.scrollWidth - main.clientWidth), documentOverflow: Math.max(0, document.documentElement.scrollHeight - innerHeight), listBottom: Math.max(0, rect.bottom - innerHeight), listVisible: list.clientHeight > 40, listScrollable: list.scrollHeight > list.clientHeight };
    })).toEqual({ mainOverflowY: 0, mainOverflowX: 0, documentOverflow: 0, listBottom: 0, listVisible: true, listScrollable: true });
    await list.hover();
    await page.mouse.wheel(0, 800);
    await expect.poll(() => list.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    await list.evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(list.locator('.agent-schedule-history-run')).toHaveCount(40);
    await expect(page.locator('.agent-schedule-detail-main')).toHaveJSProperty('scrollTop', 0);
    await detail.getByRole('button', { name: '查看全文' }).click();
    const dialog = page.getByRole('dialog', { name: '业务提示词全文' });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('p')).toHaveText(prompt);
    await expect.poll(() => dialog.locator('p').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    await expect(detail.getByRole('button', { name: '查看全文' })).toBeFocused();
    const headerBoxes = await detail.locator(':scope>header h3, :scope>header .agent-schedule-actions').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
    expect(Math.abs(headerBoxes[0].top + headerBoxes[0].height / 2 - headerBoxes[1].top - headerBoxes[1].height / 2)).toBeLessThan(2);
    await page.screenshot({ path: `../../.tmp/ops32/schedule-detail-${viewport.width}-${viewport.height}.png` });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await detail.screenshot({ path: test.info().outputPath('schedule-detail.png') });
  await page.screenshot({ path: '../../.tmp/ops32/schedule-detail.png' });
  await detail.getByRole('button', { name: '查看全文' }).click();
  await page.getByRole('button', { name: '关闭业务提示词' }).click();
  await page.locator('.agent-schedule-tree-select').nth(1).click();
  await expect(detail.locator('.agent-schedule-prompt')).toContainText('复核周报。');
  await expect(detail.getByRole('button', { name: '查看全文' })).toHaveCount(0);
  await detail.locator('.agent-schedule-history-run').first().click();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
});

test('schedule filter expands inside the sidebar without covering tasks', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await tasks.getByRole('button', { name: '筛选定时任务', exact: true }).click();
  const filterMenu = tasks.locator('.agent-schedule-filter-menu');
  const firstTask = tasks.locator('.agent-schedule-tree-group').first();
  await expect(filterMenu).toBeVisible();

  const [filterBox, taskBox] = await Promise.all([filterMenu.boundingBox(), firstTask.boundingBox()]);
  expect(filterBox).not.toBeNull();
  expect(taskBox).not.toBeNull();
  expect(filterBox!.y + filterBox!.height).toBeLessThanOrEqual(taskBox!.y);

  await filterMenu.getByRole('menuitemradio', { name: '已暂停', exact: true }).click();
  await expect(tasks.locator('.agent-schedule-tree-group')).toHaveCount(0);
  await tasks.getByRole('button', { name: '筛选定时任务', exact: true }).click();
  await tasks.locator('.agent-schedule-filter-menu').getByRole('menuitemradio', { name: '全部状态', exact: true }).click();
  await expect(tasks.locator('.agent-schedule-tree-group')).toHaveCount(2);
});

test('scheduled session shows session configuration and expands additional runs', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();

  const tasks = page.getByRole('region', { name: '会话定时任务' });
  const firstSchedule = tasks.locator('.agent-schedule-tree-group').first();
  await firstSchedule.locator('.agent-schedule-tree-select').click();
  await firstSchedule.locator('.agent-schedule-tree-toggle').click();
  await expect(tasks.locator('.agent-schedule-run')).toHaveCount(3);
  await expect(firstSchedule.getByRole('button', { name: '展开其余 2 个会话', exact: true })).toBeVisible();

  const firstRun = firstSchedule.locator('.agent-schedule-run').first();
  await firstRun.hover();
  await expect(firstRun).toHaveCSS('background-color', 'rgb(227, 241, 230)');
  await expect(firstRun.locator('.agent-workspace-conversation-select')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');

  await page.goto('/agent/schedules/conversations/scheduled-run-5');
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-5$/);
  await expect(firstSchedule.locator('[data-binding-id="scheduled-run-5"]')).toBeVisible();
  await expect(firstSchedule.getByRole('button', { name: '收起会话', exact: true })).toBeVisible();
  await page.reload();
  await expect(firstSchedule.locator('[data-binding-id="scheduled-run-5"]')).toBeVisible();
  await expect(tasks.locator('.agent-schedule-run')).toHaveCount(5);

  await tasks.locator('.agent-schedule-run').first().click();
  await expect(page.getByRole('button', { name: '会话配置', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '会话配置', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '会话配置' })).toBeVisible();
});

for (const mode of ['draft', 'existing', 'preset'] as const) {
  test(`platform-managed history MCP stays hidden in ${mode} configuration`, async ({ page }) => {
    await openWorkspace(page);
    await page.route('**/api/v1/user-configurable-capabilities', route => json(route, [
      { id: 'optional-skill', capability_key: '可选技能', capability_type: 'SKILL', is_latest: true },
    ]));
    let registrations = 0;
    await page.route('**/capabilities/batch', route => { registrations += 1; return json(route, {}); });
    await page.route('**/mcp-readiness', () => { throw new Error('Hidden MCP must not use generic credential-free probing'); });
    if (mode === 'existing') {
      await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations/scheduled-conversation', route => json(route, {
        id: 'scheduled-conversation', lifecycle: 'ACTIVE', display_title: '日报会话',
        streaming_callback_ready: true, write_available: true, created_at: now, updated_at: now,
        capabilities: [],
      }));
      await page.goto('/agent/conversations/scheduled-conversation');
      await page.getByRole('button', { name: '会话配置', exact: true }).click();
    } else if (mode === 'preset') {
      await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
      await page.getByRole('region', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
      await page.getByRole('button', { name: /配置会话/ }).click();
    } else {
      await page.getByRole('button', { name: '新会话配置', exact: true }).click();
    }
    const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
    await expect(dialog.getByText('会话历史查询', { exact: true })).toHaveCount(0);
    await expect(dialog.getByText('flowweave-reference-history', { exact: true })).toHaveCount(0);
    await dialog.getByRole('button', { name: 'MCP', exact: true }).click();
    await expect(dialog.getByText('没有匹配的已发布能力。', { exact: true })).toBeVisible();
    await dialog.getByPlaceholder('搜索名称、说明或文件…').fill('history');
    await expect(dialog.getByText('没有匹配的已发布能力。', { exact: true })).toBeVisible();
    await dialog.getByPlaceholder('搜索名称、说明或文件…').fill('');
    await dialog.getByRole('button', { name: 'all', exact: true }).click();
    await expect(dialog.getByText('可选技能', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: mode === 'existing' ? '保存' : mode === 'preset' ? '保存会话配置' : '用于新建会话', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(registrations).toBe(0);
  });
}

test('capability configuration toggles visible selections and filters to selected capabilities', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
  await page.getByRole('region', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
  await page.getByRole('button', { name: /配置会话/ }).click();

  const dialog = page.getByRole('dialog', { name: '会话配置' });
  const selectVisible = dialog.getByRole('button', { name: '选择筛选结果（2）', exact: true });
  await expect(selectVisible).toHaveAttribute('aria-pressed','false');
  await expect(selectVisible.locator('svg')).toHaveClass(/lucide-square$/);
  await expect(dialog.getByText(/已选择 \d+ 项/, { exact: true })).toHaveCount(0);
  await selectVisible.click();
  await expect(dialog.getByRole('button', { name: '取消选择筛选结果', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '取消选择筛选结果', exact: true })).toHaveAttribute('aria-pressed','true');
  await expect(dialog.getByRole('button', { name: '取消选择筛选结果', exact: true }).locator('svg')).toHaveClass(/lucide-square-check/);

  await dialog.getByRole('button', { name: '只看已选', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '显示全部能力', exact: true })).toBeVisible();
  await expect(dialog.getByText('日报技能', { exact: true })).toBeVisible();
  await expect(dialog.getByText('周报技能', { exact: true })).toBeVisible();

  await dialog.getByRole('button', { name: '取消选择筛选结果', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '会话历史查询', exact: true })).toBeDisabled();
  await expect(dialog.getByText(/已选择 \d+ 项/, { exact: true })).toHaveCount(0);
  await expect(dialog.locator('.agent-capability-select-visible')).toHaveAttribute('aria-pressed','false');
  await expect(dialog.locator('.agent-capability-select-visible svg')).toHaveClass(/lucide-square$/);
  await expect(dialog.getByRole('button', { name: '显示全部能力', exact: true })).toBeVisible();
});

for (const mode of ['create', 'edit'] as const) {
  test(`schedule ${mode} configures all capability types and credentials before runtime starts`, async ({ page }) => {
    const types = ['MCP', 'CONTEXT', 'PLUGIN', 'SKILL', 'AGENT_DEFINITION', 'HOOK'];
    const ids = types.map(type => `preset-${type}`);
    const writes = await openWorkspace(page, [], false, '日报会话', { capabilityVersionIds: mode === 'edit' ? ['old-MCP', 'old-CONTEXT'] : [], credentialIds: ['credential-1'] });
    const catalog = types.map(type => ({ id: `preset-${type}`, capability_key: `预设${type}`, capability_type: type, description: '可预设的能力', filename: 'preset.json', is_latest: true, revision_number: 2 }));
    await page.route('**/api/v1/capabilities', route => json(route, [...catalog, ...['MCP', 'CONTEXT'].map(type => ({ ...catalog.find(item => item.capability_type === type)!, id: `old-${type}`, is_latest: false, revision_number: 1 }))]));
    await page.route('**/api/v1/capability-collections', route => json(route, [{ id: 'skills-only', name: '技能组合', members: [catalog.find(item => item.capability_type === 'SKILL')] }]));
    let readinessRequests = 0;
    await page.route('**/mcp-readiness**', route => { readinessRequests += 1; return json(route, { state: 'UNAVAILABLE', error_kind: 'connection' }); });
    await page.route('**/website-credentials', route => json(route, [1, 2].map(n => ({ id: `credential-${n}`, name: `认证${n}`, target_host: 'example.test', target_path: '/', auth_type: 'TOKEN' }))));
    await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
    const tasks = page.getByRole('region', { name: '会话定时任务' });
    if (mode === 'edit') {
      await tasks.locator('.agent-schedule-tree-select').first().click();
      await page.getByRole('button', { name: '编辑', exact: true }).click();
    } else {
      await tasks.getByRole('button', { name: '新建', exact: true }).click();
      await page.getByLabel('任务名称', { exact: true }).fill('完整预配置');
      await page.getByLabel('业务提示词').fill('按预设运行');
    }
    await page.getByRole('button', { name: /配置会话/ }).click();
    const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
    const configNavigation = dialog.getByRole('navigation', { name: '会话配置分类' });
    await expect(configNavigation.getByRole('button', { name: '能力', exact: true })).toHaveAttribute('aria-current', 'page');
    const collectionTrigger = dialog.getByRole('button', { name: 'Skill 组合', exact: true });
    await expect(dialog.getByRole('region', { name: 'Skill 组合筛选' })).toHaveCount(0);
    await collectionTrigger.click();
    await expect(dialog.getByRole('region', { name: 'Skill 组合筛选' })).toBeVisible();
    await dialog.getByRole('button', { name: '技能组合', exact: true }).click();
    await dialog.getByRole('button', { name: '清除筛选', exact: true }).click();
    await collectionTrigger.click();
    await expect(dialog.getByRole('region', { name: 'Skill 组合筛选' })).toHaveCount(0);
    for (const [type, tab] of types.map((type, index) => [type, ['MCP', 'context', 'plugin', 'skill', 'agent', 'hook'][index]])) {
      await dialog.getByRole('button', { name: tab, exact: true }).click();
      if (mode === 'edit' && ['MCP', 'CONTEXT'].includes(type)) {
        const matchingRows = dialog.locator('.agent-capability-list > button').filter({ has: page.locator('b', { hasText: new RegExp(`^预设${type}$`) }) });
        const old = type === 'CONTEXT'
          ? matchingRows.filter({ has: page.locator('.agent-capability-version-badge') })
          : matchingRows.filter({ has: page.locator('.agent-selection-checkbox svg') });
        if (type === 'MCP') await expect(matchingRows).toHaveCount(2);
        await expect(old).toHaveClass(/selected/);
        await expect(old).toBeEnabled();
        if (type === 'CONTEXT') {
          await expect(dialog.locator('.agent-capability-list > button')).toHaveCount(1);
          await expect(old.locator('.agent-capability-version-badge')).toHaveText('v1');
          await expect(old.locator('.agent-capability-sync')).toHaveCount(0);
        }
        await old.click();
        if (type === 'CONTEXT') await expect(matchingRows.filter({ has: page.locator('.agent-capability-version-badge') })).toHaveCount(0);
      }
      const current = dialog.locator('.agent-capability-list > button').filter({ has: page.locator('b', { hasText: new RegExp(`^预设${type}$`) }) });
      await current.first().click();
    }
    await dialog.getByRole('button', { name: '认证', exact: true }).click();
    await expect(configNavigation.getByRole('button', { name: '认证', exact: true })).toHaveAttribute('aria-current', 'page');
    if (mode === 'edit') await dialog.getByRole('button', { name: '全选认证', exact: true }).click();
    await expect(dialog.getByText('已选择 2 项认证', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '取消全选认证', exact: true }).click();
    await expect(dialog.getByText('已选择 0 项认证', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: /认证1/ }).click();
    const credentialMark = dialog.locator('.agent-capability-list > button i').first();
    expect(await credentialMark.evaluate(element => { const s = getComputedStyle(element); return [s.width, s.height, s.borderRadius]; })).toEqual(['16px', '16px', '4px']);
    await dialog.screenshot({ path: `../../.tmp/fr648-${mode}-credentials.png` });
    await page.setViewportSize({ width: 640, height: 640 });
    await expect(dialog.getByRole('button', { name: '全选认证', exact: true })).toBeInViewport();
    await expect(dialog.getByRole('button', { name: '保存认证配置', exact: true })).toBeInViewport();
    expect(await credentialMark.evaluate(element => getComputedStyle(element).width)).toBe('16px');
    await dialog.screenshot({ path: `../../.tmp/fr648-${mode}-credentials-mobile.png` });
    await page.setViewportSize({ width: 1440, height: 1000 });
    // Saving from the authentication tab must preserve edits on both tabs.
    await dialog.getByRole('button', { name: '保存认证配置', exact: true }).click();
    await page.getByRole('button', { name: /配置会话/ }).click();
    await expect(dialog.locator('.agent-capability-list > button.selected')).toHaveCount(7);
    await dialog.getByRole('button', { name: '保存会话配置', exact: true }).click();
    await page.getByRole('button', { name: '保存任务', exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toMatchObject({ method: mode === 'edit' ? 'PUT' : 'POST', body: { capability_version_ids: ids, credential_ids: ['credential-1'] } });
    expect(readinessRequests).toBe(0);
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('button', { name: /配置会话/ }).click();
    await expect(dialog.locator('.agent-capability-list > button.selected')).toHaveCount(7);
    await dialog.getByRole('button', { name: '认证', exact: true }).click();
    await expect(dialog.getByText('已选择 1 项认证', { exact: true })).toBeVisible();
  });
}

test('credential select all excludes synced credentials and disables itself when nothing remains', async ({ page }) => {
  await openWorkspace(page);
  let entries = ['CURRENT', 'NEW', 'MODIFIED', 'UNRECORDED'].map((state, index) => ({ id: `credential-${index}`, name: `同步认证${index}`, target_host: 'example.test', target_path: '/', auth_type: 'TOKEN', sync_state: state }));
  const syncWrites: unknown[] = [];
  await page.route('**/credential-sync', route => {
    if (route.request().method() === 'POST') {
      syncWrites.push(route.request().postDataJSON());
      entries = entries.map(item => ({ ...item, sync_state: 'CURRENT' }));
    }
    return json(route, { initialized_at: now, credentials: entries });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '会话配置', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '会话配置' });
  await dialog.getByRole('button', { name: '认证', exact: true }).click();
  const credentialHeading = dialog.locator('.agent-credential-sync-heading');
  const selectAllCredentials = dialog.getByRole('button', { name: '全选认证', exact: true });
  expect(await credentialHeading.evaluate(element => getComputedStyle(element).fontSize)).toBe('11px');
  expect(await selectAllCredentials.evaluate(element => { const style = getComputedStyle(element); return [style.width, style.height]; })).toEqual(['22px', '22px']);
  await expect(dialog.getByRole('button', { name: '同步认证0（已同步）', exact: true })).toBeDisabled();
  await expect(dialog.getByText('已选择 2 项认证', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '全选认证', exact: true }).click();
  await expect(dialog.getByText('已选择 3 项认证', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '取消全选认证', exact: true }).click();
  await expect(dialog.getByText('已选择 0 项认证', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '全选认证', exact: true }).click();
  await dialog.getByRole('button', { name: '同步认证', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '全选认证', exact: true })).toBeDisabled();
  expect(syncWrites).toEqual([{ credential_ids: ['credential-1', 'credential-2', 'credential-3'] }]);
});

test('capability filtering keeps dialog and footer geometry stable', async ({ page }) => {
  await openWorkspace(page);
  await page.route('**/api/v1/capabilities', route => json(route,Array.from({length:30},(_,index)=>({id:`fixed-cap-${index}`,capability_key:`技能 ${index}`,capability_type:'SKILL',description:'用于验证列表内部滚动',filename:`skill-${index}.md`,is_latest:true}))));
  await page.getByRole('navigation',{name:'Agent 工作区导航'}).getByRole('button',{name:'定时任务'}).click();
  await page.getByRole('region',{name:'会话定时任务'}).getByRole('button',{name:'新建',exact:true}).click();
  await page.getByRole('button',{name:/配置会话/}).click();
  const dialog=page.getByRole('dialog',{name:'会话配置'});
  const geometry=()=>dialog.evaluate(element=>{
    const box=element.getBoundingClientRect();
    const footer=element.querySelector('.agent-session-config-panel > footer')!.getBoundingClientRect();
    return {top:box.top,height:box.height,footer:footer.top};
  });
  for(const height of [1000,600]){
    await page.setViewportSize({width:1440,height});
    const before=await geometry();
    await expect.poll(()=>dialog.locator('.agent-capability-list').evaluate(element=>element.scrollHeight>element.clientHeight)).toBe(true);
    await dialog.getByRole('button',{name:'只看已选',exact:true}).click();
    await expect(dialog.getByRole('button', { name: '会话历史查询', exact: true })).toBeDisabled();
    expect(await geometry()).toEqual(before);
    await dialog.getByRole('button',{name:'显示全部能力',exact:true}).click();
    expect(await geometry()).toEqual(before);
    const search=dialog.getByPlaceholder('搜索名称、说明或文件…');
    await search.fill('不存在的能力');
    await expect(dialog.getByText('没有匹配的已发布能力。')).toBeVisible();
    expect(await geometry()).toEqual(before);
    await search.fill('');
    await expect(dialog.locator('.agent-capability-list > button')).toHaveCount(31);
    await expect(dialog.getByRole('button',{name:'保存会话配置',exact:true})).toBeInViewport();
  }
});

for (const mode of ['existing', 'draft'] as const) {
  test(`capability collections keep the summary and first item unobstructed in ${mode} sessions`, async ({ page }) => {
    await openWorkspace(page);
    const capabilities = Array.from({ length: 30 }, (_, index) => ({
      id: `layout-cap-${index}`, capability_key: `布局技能 ${index}`, capability_type: 'SKILL',
      description: '验证统计栏下方的能力条目完整可见并能正常选择', filename: `layout-${index}.md`, is_latest: true,
    }));
    await page.route('**/api/v1/capabilities', route => json(route, capabilities));
    await page.route('**/api/v1/capability-collections', route => json(route, [
      { id: 'layout-collection', name: '常用技能组合', members: capabilities },
    ]));
    if (mode === 'existing') {
      await page.goto('/agent/conversations/scheduled-conversation');
      await page.getByRole('button', { name: '会话配置', exact: true }).click();
    } else {
      await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
      await page.getByRole('region', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
      await page.getByRole('button', { name: /配置会话/ }).click();
    }
    const dialog = page.getByRole('dialog', { name: '会话配置' });
    const collectionTrigger = dialog.locator('.agent-capability-collection-trigger');
    await expect(collectionTrigger).toHaveAccessibleName('Skill 组合');
    await expect(collectionTrigger).toBeVisible();
    await expect(dialog.getByRole('button', { name: '常用技能组合', exact: true })).toHaveCount(0);
    const assertLayout = async () => {
      const layout = await dialog.evaluate(element => {
        const bounds = (selector: string) => element.querySelector<HTMLElement>(selector)!.getBoundingClientRect();
        const list = bounds('.agent-session-config-panel > .agent-capability-list');
        const first = bounds('.agent-capability-list > button');
        const footer = bounds('.agent-session-config-panel > footer');
        const firstButton = element.querySelector('.agent-capability-list > button')!;
        const hit = document.elementFromPoint(first.x + first.width / 2, first.y + first.height / 2);
        return { listTop: list.top, firstTop: first.top, listBottom: list.bottom, footerTop: footer.top,
          firstClickable: hit !== null && firstButton.contains(hit),
          scrollable: element.querySelector('.agent-capability-list')!.scrollHeight > list.height };
      });
      expect(layout.listTop).toBeLessThanOrEqual(layout.firstTop);
      expect(layout.listBottom).toBeLessThanOrEqual(layout.footerTop + 1);
      expect(layout.firstClickable).toBe(true);
      expect(layout.scrollable).toBe(true);
      await expect(dialog.locator('.agent-session-config-panel > footer')).toBeInViewport();
    };
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 1440, height: 600 }, { width: 640, height: 640 }]) {
      await page.setViewportSize(viewport);
      await assertLayout();
      await dialog.getByRole('button', { name: 'MCP', exact: true }).click();
      await expect(dialog.locator('.agent-capability-collection-filters')).toHaveCount(0);
      await dialog.getByRole('button', { name: 'all', exact: true }).click();
      await assertLayout();
    }
    await collectionTrigger.click();
    await dialog.getByRole('button', { name: '常用技能组合', exact: true }).click();
    await expect(collectionTrigger).toHaveAccessibleName('Skill 组合 · 1');
    const optional = dialog.getByRole('button', { name: /布局技能 0/ });
    await optional.click();
    await expect(optional).toHaveClass(/selected/);
    await page.setViewportSize({ width: 1440, height: 900 });
    await dialog.screenshot({ path: test.info().outputPath(`capability-layout-${mode}.png`) });
  });
}

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

  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
  await navigation.getByRole('button', { name: '主页' }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
  await navigation.getByRole('button', { name: '主页' }).click();
  await recoverDraft.click();
  await expect(page).toHaveURL(/\/agent\?draft_work_directory_id=existing-directory$/);
  await expect(composer).toHaveValue('保留已有项目目录中的草稿');
  await expect(recoverDraft).toHaveAttribute('aria-current', 'page');
});



test.describe('China-time scheduling', () => {
  test.use({ timezoneId: 'America/New_York' });

  test('schedule times stay in China time and cron fields are saved unchanged', async ({ page }) => {
    const writes = await openWorkspace(page);
    await page.goto('/agent/schedules');
    await page.locator('.agent-schedule-tree-select').first().click();
    const summary = page.locator('.agent-schedule-summary');
    await expect(summary).toContainText('2026/10/20 09:00:00');
    await page.locator('.agent-schedule-detail').getByRole('button', { name: '编辑', exact: true }).click();
    const editor = page.locator('.agent-schedule-create');
    await expect(editor.getByLabel('Cron 表达式')).toHaveValue('0 9 * * 1-5');
    await editor.getByLabel('Cron 表达式').fill('0 10 * * *');
    await editor.getByRole('button', { name: '保存任务', exact: true }).click();
    expect(writes[0]).toMatchObject({ method: 'PUT', id: 'daily-schedule', body: { cron_expression: '0 10 * * *', expected_row_version: 1 } });
    await expect(summary).toContainText('Cron · 0 10 * * *');
  });
});

test('scheduled conversation drafts survive creating tasks and editing from task details', async ({ page }) => {
  const writes = await openWorkspace(page);
  await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
  await page.locator('.agent-schedule-tree-select').first().click();
  await expect(page.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveCount(0);
  const tasks = page.getByRole('region', { name: '会话定时任务' });
  await tasks.getByRole('button', { name: '新建', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新建定时任务' })).toBeVisible();
  await page.locator('.agent-schedule-create').getByRole('button', { name: '取消', exact: true }).click();
  await page.locator('.agent-schedule-tree-toggle').first().click();
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
  await tasks.locator('.agent-schedule-tree-select').filter({ hasText: '每周复核' }).click();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
  await page.locator('.agent-schedule-detail').getByRole('button', { name: '编辑', exact: true }).click();
  const edit = page.locator('.agent-schedule-create');
  await expect(edit).toBeVisible();
  await expect(edit.getByRole('textbox', { name: '任务名称', exact: true })).toHaveValue('每周复核');
  await edit.getByLabel('业务提示词').fill('更新后的周报复核');
  await edit.getByRole('button', { name: '保存任务', exact: true }).click();
  await expect(edit).toHaveCount(0);
  expect(writes[1]).toMatchObject({ method: 'PUT', id: 'weekly-schedule', body: { prompt: '更新后的周报复核', expected_row_version: 7 } });
  await page.locator('.agent-schedule-run').first().click();
  await expect(composer).toHaveValue('保留当前会话草稿');
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await page.reload();
  await expect(tasks).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
  await page.goto('/agent/schedules');
  await page.locator('.agent-schedule-tree-select').first().click();
  await expect(page.getByRole('heading', { name: '每日项目检查', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/schedules$/);
});

test('many subagents stay inside the floating card without scrolling it', async ({ page }) => {
  const tasks = Array.from({ length: 37 }, (_, index) => {
    const actionId = `card-task-${index}`;
    const toolCallId = `card-task-call-${index}`;
    return [
      { id: actionId, event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'card-user', action_id: actionId, tool_call_id: toolCallId, event_name: 'TaskAction', runtime_task: { phase: 'REQUESTED', action_event_id: actionId, tool_call_id: toolCallId, subagent_type: 'general-purpose', description: `卡片任务 ${index + 1}` }, timestamp: now } },
      { id: `${actionId}-result`, event_type: 'TOOL_RESULT', payload: { source: 'environment', parent_id: actionId, action_id: actionId, tool_call_id: toolCallId, event_name: 'TaskObservation', runtime_task: { phase: 'COMPLETED', action_event_id: actionId, tool_call_id: toolCallId, task_id: `task-${index}`, status: 'completed' }, timestamp: now } },
    ];
  }).flat();
  await openWorkspace(page, [
    { id: 'card-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '检查子智能体卡片', timestamp: now } },
    ...tasks,
    { id: 'card-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: tasks.at(-1)!.id, content: '检查完成。', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const card = page.locator('.agent-workspace-summary');
  const avatars = card.locator('.agent-workspace-subagent-avatars');
  const summary = card.locator('.agent-workspace-subagent-summary');
  const geometry = () => card.evaluate(element => {
    const cardRect = element.getBoundingClientRect();
    const overview = element.querySelector('.agent-workspace-overview')!;
    const row = element.querySelector('.agent-workspace-subagents')!;
    const targets = [overview, row, row.querySelector('button')!, row.querySelector('b')!];
    return {
      overflow: Math.max(0, ...targets.map(target => { const rect = target.getBoundingClientRect(); return Math.max(cardRect.left - rect.left, rect.right - cardRect.right); })),
      horizontalOverflow: element.scrollWidth - element.clientWidth,
      overflowStyles: [element, overview].map(target => { const style = getComputedStyle(target); return [style.overflowX, style.overflowY]; }),
      scroll: [element.scrollLeft, element.scrollTop, overview.scrollLeft, overview.scrollTop],
    };
  });
  const expectContained = async () => {
    await settleWorkspaceLayout(page);
    await expect(summary).not.toHaveAttribute('aria-label');
    await expect(summary.locator('b')).toHaveText('查看全部 (37)');
    await expect.poll(async () => (await geometry()).overflow).toBeLessThanOrEqual(1);
    expect((await geometry()).horizontalOverflow).toBe(0);
    expect((await geometry()).overflowStyles.flat().every(value => value === 'clip')).toBe(true);
    expect(await avatars.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    await card.evaluate(element => {
      element.scrollTo(999, 999);
      element.querySelector('.agent-workspace-overview')!.scrollTo(999, 999);
    });
    expect((await geometry()).scroll).toEqual([0, 0, 0, 0]);
  };
  await expect(avatars.locator('.subagent-avatar')).toHaveCount(37);
  for (const [width, height] of [[1920, 1080], [1440, 900], [1024, 640], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expectContained();
    await page.screenshot({ path: `../../.tmp/fr-651-card-${width}.png` });
    if (width === 390) await card.screenshot({ path: '../../.tmp/fr-651-card-mobile.png' });
  }
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).first().click();
  await expectContained();
  await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
  await page.getByRole('button', { name: '日报会话', exact: true }).last().click();
  await expectContained();
  await page.screenshot({ path: '../../.tmp/fr-651-card-fullscreen.png' });
  await card.getByRole('button', { name: '查看全部 37 个子智能体任务' }).click();
  await expect(page.locator('.agent-subagent-task-list button')).toHaveCount(37);
  await expect(page.locator('.agent-subagent-record')).toContainText('卡片任务 1');
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

for (const initiallyOpen of [true, false]) {
  test(`workspace tools temporarily hide the card without changing the ${initiallyOpen ? 'enabled' : 'disabled'} global preference`, async ({ page }) => {
    await openWorkspace(page, [], true);
    await page.goto('/agent/conversations/scheduled-conversation');
    const card = page.locator('.agent-workspace-summary');
    if (!initiallyOpen) await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
    const preference = () => page.evaluate(() => localStorage.getItem('flowweave:environment-card-open') !== 'false');
    const expectRestored = async () => {
      await expect(card).toHaveAttribute('aria-hidden', String(!initiallyOpen));
      if (initiallyOpen) await expect(card).toBeVisible(); else await expect(card).toBeHidden();
      expect(await preference()).toBe(initiallyOpen);
    };
    await expectRestored();
    for (const close of ['last-tab', 'panel', 'fullscreen']) {
      await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
      await expect(card).toBeHidden();
      expect(await preference()).toBe(initiallyOpen);
      if (close === 'last-tab') {
        await page.getByRole('button', { name: '周报会话', exact: true }).click();
        await expect(page).toHaveURL(/scheduled-conversation-2$/);
        await expectRestored();
        await page.getByRole('button', { name: '日报会话', exact: true }).click();
        await expect(page).toHaveURL(/scheduled-conversation$/);
        await expect(page.locator('.agent-workspace-drawer')).toHaveClass(/tools-open/);
        await expect(card).toBeHidden();
        await page.getByRole('button', { name: '关闭新标签页页签', exact: true }).click();
      } else {
        if (close === 'fullscreen') await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
        await page.getByRole('button', { name: '关闭工作区工具', exact: true }).click();
      }
      await expect(page.locator('.agent-workspace-drawer')).toHaveClass(/summary-open/);
      await expectRestored();
    }
    await page.reload();
    await expectRestored();
  });
}

for (const initiallyOpen of [true, false]) {
  test(`manual card toggles in split view preserve the ${initiallyOpen ? 'enabled' : 'disabled'} global preference`, async ({ page }) => {
    await openWorkspace(page, [], true);
    await page.goto('/agent/conversations/scheduled-conversation');
    const card = page.locator('.agent-workspace-summary');
    if (!initiallyOpen) await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).click();
    const expectCard = async (visible: boolean) => {
      await expect(card).toHaveAttribute('aria-hidden', String(!visible));
      if (visible) await expect(card).toBeVisible(); else await expect(card).toBeHidden();
      await expect(page.getByRole('button', { name: visible ? '隐藏悬浮信息卡' : '显示悬浮信息卡', exact: true }).first()).toHaveAttribute('aria-pressed', String(visible));
      expect(await page.evaluate(() => localStorage.getItem('flowweave:environment-card-open') !== 'false')).toBe(initiallyOpen);
    };
    for (const close of ['last-tab', 'panel', 'fullscreen']) {
      await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
      await expectCard(false);
      await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).first().click();
      await expectCard(true);
      await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).first().click();
      await expectCard(false);
      if (!initiallyOpen) {
        await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).first().click();
        await expectCard(true);
      }
      if (close === 'last-tab') {
        await page.getByRole('button', { name: '关闭新标签页页签', exact: true }).click();
      } else {
        if (close === 'fullscreen') await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
        await page.getByRole('button', { name: '关闭工作区工具', exact: true }).click();
      }
      await expect(page.locator('.agent-workspace-drawer')).toHaveClass(/summary-open/);
      await expectCard(initiallyOpen);
      await page.getByRole('button', { name: '周报会话', exact: true }).click();
      await expect(page).toHaveURL(/scheduled-conversation-2$/);
      await expectCard(initiallyOpen);
      await page.getByRole('button', { name: '日报会话', exact: true }).click();
      await expect(page).toHaveURL(/scheduled-conversation$/);
      await expectCard(initiallyOpen);
    }
    await page.reload();
    await expectCard(initiallyOpen);
  });
}

test('the floating workspace card remains absent until workspace details load', async ({ page }) => {
  let releaseWorkspaceDetails: (() => void) | undefined;
  const workspaceDetailsReady = new Promise<void>(resolve => { releaseWorkspaceDetails = resolve; });

  await openWorkspace(page);
  // Conversation workspace reads include binding_id; delay both forms.
  await page.route(/\/agent-workspaces\/schedule-workspace\/workspace(?:\?.*)?$/, async route => {
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

test('composer newlines keep the first line at the single-line top inset', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  const geometry = () => composer.evaluate(element => {
    const textarea = element as HTMLTextAreaElement;
    const styles = getComputedStyle(textarea);
    return {
      topInset: textarea.getBoundingClientRect().top - textarea.closest('.agent-composer')!.getBoundingClientRect().top
        + parseFloat(styles.paddingTop) - textarea.scrollTop,
      height: textarea.clientHeight,
      lineHeight: parseFloat(styles.lineHeight),
      padding: parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom),
      scrollTop: textarea.scrollTop,
      overflowY: styles.overflowY,
    };
  });
  await composer.fill('1');
  const single = await geometry();
  for (let lines = 2; lines <= 10; lines += 1) {
    await composer.press('Shift+Enter');
    await expect(composer).toHaveValue(`1${'\n'.repeat(lines - 1)}`);
    await expect.poll(async () => (await geometry()).height).toBeGreaterThanOrEqual(single.lineHeight * lines + single.padding - 1);
    await expect.poll(async () => (await geometry()).topInset).toBeCloseTo(single.topInset, 0);
    expect((await geometry()).scrollTop).toBe(0);
  }
  await composer.press('Shift+Enter');
  await expect.poll(async () => (await geometry()).overflowY).toBe('auto');
  await composer.fill('1\n第二行');
  await expect.poll(async () => (await geometry()).topInset).toBeCloseTo(single.topInset, 0);
  expect((await geometry()).scrollTop).toBe(0);
  await composer.fill('自动换行的文字'.repeat(45));
  await expect.poll(async () => (await geometry()).height).toBeGreaterThan(single.height);
  expect((await geometry()).topInset).toBeCloseTo(single.topInset, 0);
  await composer.fill('1');
  await composer.press('Shift+Enter');
  await page.screenshot({ path: '../../.tmp/composer-newline.png' });
});

test('assistant messages render inline and display LaTex formulas', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'formula-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '计算盘前涨跌幅', timestamp: now } },
    { id: 'formula-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'formula-user', content: '行内公式 $E = mc^2$。\n\n$$\n\\frac{\\text{盘前最新价} - \\text{preClose}}{\\text{preClose}}\n$$', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');

  const answer = page.locator('.conversation-message.assistant');
  await expect(answer.locator('.katex')).toHaveCount(2);
  await expect(answer.locator('.katex-display')).toBeVisible();
  await expect(answer).toContainText('行内公式');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  const copyButtons = answer.getByRole('button', { name: '复制公式' });
  await expect(copyButtons).toHaveCount(2);
  await copyButtons.nth(0).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('$E = mc^2$');
  await copyButtons.nth(1).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('$$\n\\frac{\\text{盘前最新价} - \\text{preClose}}{\\text{preClose}}\n$$');
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

test('double-clicking a workspace tab toggles fullscreen tools', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();

  const tabs = page.getByRole('navigation', { name: '工作区页签' });
  const launcherTab = tabs.getByRole('button', { name: '新标签页', exact: true });
  await launcherTab.dblclick();
  await expect(page.locator('.agent-workbench-main')).toBeHidden();
  await expect(page.getByRole('button', { name: '退出全屏', exact: true })).toBeVisible();

  await launcherTab.dblclick();
  await expect(page.locator('.agent-workbench-main')).toBeVisible();
  await expect(page.getByRole('button', { name: '全屏查看工作区工具', exact: true })).toBeVisible();
});

test('workspace tabs share default hover and active states in split and fullscreen views', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  const tabs = page.getByRole('navigation', { name: '工作区页签' });
  await tabs.getByRole('button', { name: '新建工作区页签', exact: true }).click();
  const launchers = tabs.getByRole('button', { name: '新标签页', exact: true });
  const appearance = (index: number) => launchers.nth(index).evaluate(button => {
    const tab = getComputedStyle(button.parentElement!);
    return { background: tab.backgroundColor, shadow: tab.boxShadow, color: tab.color, buttonBackground: getComputedStyle(button).backgroundColor };
  });
  let reference: Record<string, Awaited<ReturnType<typeof appearance>>> | undefined;
  for (const mode of ['split', 'fullscreen', 'split']) {
    if (mode === 'fullscreen') await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
    else if (reference) await page.getByRole('button', { name: '退出全屏', exact: true }).click();
    await expect(tabs.locator(':scope > div.active')).toHaveCount(1);
    await page.mouse.move(20, 300);
    const idle = await appearance(0);
    const active = await appearance(1);
    expect(idle.background).toBe('rgba(0, 0, 0, 0)');
    expect(active.background).toBe('rgb(255, 255, 255)');
    const inactiveClose=launchers.nth(0).locator('..').locator('.agent-workspace-tab-close');
    const activeClose=launchers.nth(1).locator('..').locator('.agent-workspace-tab-close');
    await expect(inactiveClose).toHaveCSS('opacity','0');
    await expect(inactiveClose).toHaveCSS('pointer-events','none');
    await expect(activeClose).toHaveCSS('opacity','1');
    const inactiveWidth=await launchers.nth(0).locator('..').evaluate(element=>element.getBoundingClientRect().width);
    await launchers.nth(0).hover();
    await expect(inactiveClose).toHaveCSS('opacity','1');
    expect(await launchers.nth(0).locator('..').evaluate(element=>element.getBoundingClientRect().width)).toBe(inactiveWidth);
    const hover = await appearance(0);
    expect(hover.background).toBe('rgb(243, 247, 244)');
    expect(hover.buttonBackground).toBe('rgba(0, 0, 0, 0)');
    const states = { idle, hover, active };
    if (reference) expect(states).toEqual(reference);
    else reference = states;
    await launchers.nth(0).click();
    await expect(launchers.nth(0).locator('..')).toHaveClass('active');
    await expect(launchers.nth(1).locator('..')).not.toHaveClass('active');
    await launchers.nth(1).click();
  }
});

test('fullscreen conversation tabs cap long titles and shrink with available space', async ({ page }) => {
  const title = '这是一个超过十五个汉字容量的会话标题用于验证省略显示';
  await openWorkspace(page, [], false, title);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();

  const tab = page.getByRole('navigation', { name: '工作区页签' }).getByRole('button', { name: title, exact: true });
  await expect(tab).toHaveAttribute('title', title);
  const maximumWidth = await tab.evaluate(element => element.getBoundingClientRect().width);
  const configuredMaximum = await tab.evaluate(element => parseFloat(getComputedStyle(element.parentElement!).maxWidth));
  expect(maximumWidth).toBeLessThanOrEqual(configuredMaximum);
  await expect(tab.locator('span')).toHaveCSS('text-overflow', 'ellipsis');

  await page.setViewportSize({ width: 360, height: 900 });
  await expect.poll(() => tab.evaluate(element => element.getBoundingClientRect().width)).toBeLessThan(maximumWidth);
});

test('fullscreen tools occupy the entire workbench region and restore the conversation without extra grid rows', async ({page}) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  const composer=page.getByRole('textbox',{name:'发送 Agent 消息'});
  await composer.fill('全屏切换时保留草稿');
  await page.getByRole('button',{name:'新建工作区页签',exact:true}).first().click();
  const controls=page.locator('.agent-workspace-tool-actions');
  const controlSizes=() => controls.getByRole('button').evaluateAll(buttons => buttons.slice(-2).map(button => {
    const {width,height}=button.getBoundingClientRect();
    return {width,height};
  }));
  const splitControlSizes=await controlSizes();
  const newTab=page.getByRole('navigation',{name:'工作区页签'}).getByRole('button',{name:'新建工作区页签',exact:true});
  const expectSquareNewTab=async () => {
    const geometry=await newTab.evaluate(button => {
      const rect=button.getBoundingClientRect();
      const icon=button.querySelector('svg')!.getBoundingClientRect();
      return {width:rect.width,height:rect.height,offsetX:icon.x+icon.width/2-rect.x-rect.width/2,offsetY:icon.y+icon.height/2-rect.y-rect.height/2};
    });
    expect(geometry.width).toBe(geometry.height);
    expect(geometry.width).toBeLessThanOrEqual(32);
    expect(Math.abs(geometry.offsetX)).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.offsetY)).toBeLessThanOrEqual(1);
  };
  await expectSquareNewTab();
  await controls.screenshot({path:test.info().outputPath('split-controls.png')});
  await page.getByRole('button',{name:'全屏查看工作区工具',exact:true}).click();
  const main=page.locator('.agent-workbench-main');
  await expect(main).toBeHidden();
  expect(splitControlSizes).toEqual(await controlSizes());
  await expectSquareNewTab();
  await controls.screenshot({path:test.info().outputPath('fullscreen-controls.png')});
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

test('conversation website links open external browser tabs while internal service requests are consumed once', async ({ page, context }) => {
  const externalUrl = 'https://external.example.test/reviews/1';
  await context.route('https://external.example.test/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>External review</h1>' }));
  await openWorkspace(page, [
    { id: 'links-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看链接', timestamp: now } },
    { id: 'links-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'links-user', content: `[外部评审](${externalUrl})\n\n[工作区服务](http://localhost:3000/report)\n\n[回环服务](http://127.0.0.1:8080/)`, timestamp: now } },
  ], true);
  let browserProxyRequests = 0;
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/browser/**', async route => {
    browserProxyRequests += 1;
    await route.fulfill({ contentType: 'text/html', body: '<h1>Workspace service</h1>' });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  const popupPromise = page.waitForEvent('popup');
  await page.getByRole('link', { name: '外部评审' }).click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(externalUrl);
  await expect(popup.getByRole('heading', { name: 'External review' })).toBeVisible();
  await expect(page.locator('.agent-workspace-browser-tab-panel')).toHaveCount(0);
  expect(browserProxyRequests).toBe(0);
  await popup.close();

  const panels = page.locator('.agent-workspace-browser-tab-panel');
  const composer = page.getByRole('textbox', { name: '发送 Agent 消息' });
  for (const [label, hostname] of [['工作区服务', 'localhost'], ['回环服务', '127.0.0.1']]) {
    await page.getByRole('link', { name: label, exact: true }).click();
    await expect(panels).toHaveCount(1);
    await expect(panels.locator('iframe').contentFrame().getByRole('heading', { name: 'Workspace service' })).toBeVisible();
    // Draft edits and focus reconciliation rerender the parent, just like polling.
    // Neither may replay the last open command, including after its tab is closed.
    for (let i = 0; i < 3; i += 1) {
      await composer.fill(`保留草稿 ${i}`);
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await settleWorkspaceLayout(page);
      await expect(panels).toHaveCount(1);
    }
    await page.getByRole('button', { name: `关闭${hostname}页签`, exact: true }).click();
    await expect(panels).toHaveCount(0);
    await composer.fill('关闭后继续编辑');
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await settleWorkspaceLayout(page);
    await expect(panels).toHaveCount(0);
  }
  // An explicit click may reopen it once; switching sessions must not replay it.
  await page.getByRole('link', { name: '工作区服务', exact: true }).click();
  await expect(panels).toHaveCount(1);
  await page.getByRole('button', { name: '周报会话', exact: true }).dblclick();
  await expect(page).toHaveURL(/scheduled-conversation-2$/);
  await expect(panels).toHaveCount(0);
  await page.getByRole('button', { name: '日报会话', exact: true }).dblclick();
  await expect(page).toHaveURL(/scheduled-conversation$/);
  await expect(panels).toHaveCount(1);
});

test('Runtime page external links and browser addresses open native tabs without replacing the service', async ({ page, context }) => {
  const externalUrl = 'https://external.example.test/runtime';
  await context.route('https://external.example.test/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>External destination</h1><script>document.body.dataset.scriptRan="true"</script>' }));
  await openWorkspace(page, [
    { id: 'service-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '预览服务', timestamp: now } },
    { id: 'service-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'service-user', content: '[工作区页面](http://localhost:3000/)', timestamp: now } },
  ]);
  let proxyRequests = 0;
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/browser/**', async route => {
    proxyRequests += 1;
    await route.fulfill({ contentType: 'text/html', body: `<html><body><h1>工作区服务</h1><a href="#section">页内导航</a><h2 id="section">本地章节</h2><script>
      const link=document.createElement('a');link.href='${externalUrl}';link.target='_top';link.textContent='动态外链';document.body.append(link);
      link.addEventListener('click',event=>{event.preventDefault();document.body.dataset.consumed='true';});
    </script></body></html>` });
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link', { name: '工作区页面' }).click();
  const panel = page.locator('.agent-workspace-browser-tab-panel.active');
  const frame = panel.locator('iframe').contentFrame();
  await expect(frame.getByRole('link', { name: '页内导航' })).toHaveAttribute('href', '#section');
  await expect(frame.getByRole('link', { name: '页内导航' })).not.toHaveAttribute('target', '_blank');
  const initialProxyRequests = proxyRequests;
  const mainUrl = page.url();
  for (const action of ['link', 'address']) {
    const popupPromise = page.waitForEvent('popup');
    if (action === 'link') await frame.getByRole('link', { name: '动态外链' }).click();
    else {
      await panel.getByRole('textbox', { name: '网页地址', exact: true }).fill(externalUrl);
      await panel.getByRole('textbox', { name: '网页地址', exact: true }).press('Enter');
    }
    const popup = await popupPromise;
    await expect(popup).toHaveURL(externalUrl);
    await expect(popup.locator('body')).toHaveAttribute('data-script-ran', 'true');
    expect(await popup.evaluate(() => window.opener === null)).toBe(true);
    await popup.close();
    await expect(page).toHaveURL(mainUrl);
    await expect(frame.getByRole('heading', { name: '工作区服务' })).toBeVisible();
    await expect(frame.locator('body')).not.toHaveAttribute('data-consumed', 'true');
    await expect(panel.getByRole('textbox', { name: '网页地址', exact: true })).toHaveValue('http://localhost:3000/');
    await expect(page.locator('.agent-workspace-browser-tab-panel')).toHaveCount(1);
    expect(proxyRequests).toBe(initialProxyRequests);
    expect(context.pages()).toHaveLength(1);
  }
});

test('HTML reports use a same-origin viewer URL that copied tabs reopen without Runtime proxying', async ({ page, context }) => {
  const externalUrl = 'https://external.example.test/project';
  await context.route('https://external.example.test/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>External project</h1><script>document.body.dataset.scriptRan="true"</script>' }));
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
      body: '<!doctype html><html><body><h1>会话日报</h1><a href="https://external.example.test/project" target="_self">项目链接</a><script>document.body.dataset.scriptRan="true"</script></body></html>',
    });
  });

  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link', { name: '打开 HTML 日报' }).click();
  await expect(page.getByTitle('daily.html HTML 预览').contentFrame().getByRole('heading', { name: '会话日报' })).toBeVisible();
  const openProject = async (link: ReturnType<Page['getByRole']>) => {
    const popupPromise = page.waitForEvent('popup');
    await link.click();
    const popup = await popupPromise;
    await expect(popup).toHaveURL(externalUrl);
    // The destination does not inherit the report's script-free sandbox.
    await expect(popup.locator('body')).toHaveAttribute('data-script-ran', 'true');
    expect(await popup.evaluate(() => window.opener === null)).toBe(true);
    await popup.close();
  };
  await openProject(page.getByTitle('daily.html HTML 预览').contentFrame().getByRole('link', { name: '项目链接' }));
  await expect(page).toHaveURL(/\/agent\/conversations\/scheduled-conversation$/);
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
  await openProject(viewerFrame.getByTitle('daily.html').contentFrame().getByRole('link', { name: '项目链接' }));
  await expect(address).toHaveValue(viewerUrl);
  await expect(viewerFrame.getByTitle('daily.html').contentFrame().getByRole('heading', { name: '会话日报' })).toBeVisible();
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

test('launcher opens Git history without closing the workspace tools', async ({ page }) => {
  await openWorkspace(page);
  const repository = { path: '/runtime/workspace/project', branch: 'main', head: 'abcdef123456', upstream: 'origin/main', ahead: 0, behind: 0 };
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace?**', route => json(route, {
    root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
    working_directory: '/runtime/workspace/project', files: [], repositories: [repository], runtime: {}, ide: { gateway: { supported: false } },
  }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace/git/repositories?**', route => json(route, { repositories: [repository] }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace/git/log?**', route => json(route, {
    repository,
    commits: [{ id: 'abcdef123456', short_id: 'abcdef123456', author: 'FlowWeave', date: now, subject: 'Initial commit' }],
  }));

  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: 'Git 历史', exact: true }).click();

  await expect(page.locator('.agent-workspace-tab-launcher')).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '工作区页签' }).getByRole('button', { name: '文件', exact: true })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Git' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Git 视图' }).getByRole('button', { name: '提交记录', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText('Initial commit', { exact: true })).toBeVisible();
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace/git/commit?**', route => json(route, {
    repository, commit: { id: 'abcdef123456', short_id: 'abcdef', author: 'FlowWeave', date: now, subject: 'Initial commit' }, files: [{ path: 'scripts/run.py', status: 'M' }],
  }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/workspace/git/diff?**', route => json(route, {
    path: 'scripts/run.py', diff: "@@ -1 +1 @@\n-print('old')\n+print('new')\n", truncated: false,
  }));
  await page.locator('.agent-git-log button').filter({ hasText: 'Initial commit' }).click();
  await page.getByRole('tree', { name: '提交文件树' }).getByRole('button', { name: 'run.py M' }).click();
  const diff = page.locator('.agent-git-file-diff-review');
  await expect(diff.locator('.agent-diff-split .addition')).toContainText("print('new')");
  await expect(diff.locator('.agent-diff-split .deletion')).toContainText("print('old')");
  await expect(diff.locator('.addition .hljs-string .agent-diff-inline-change')).toHaveText('new');
  await expect(diff.locator('.deletion .hljs-string .agent-diff-inline-change')).toHaveText('old');
  await expect(diff.locator('.agent-diff-split-viewport>.agent-diff-scroll-end-spacer')).toHaveCount(1);
  await diff.getByRole('button', { name: '统一', exact: true }).click();
  await expect(diff.locator('.agent-diff-unified .addition')).toContainText("print('new')");
  await expect(diff.locator('.agent-diff-unified .addition .agent-diff-inline-change')).toHaveText('new');
  await expect(diff.locator('.agent-diff-unified .deletion .agent-diff-inline-change')).toHaveText('old');
  await expect(diff.locator('.agent-diff-unified>.agent-diff-scroll-end-spacer')).toHaveCount(1);
  await diff.getByRole('button', { name: '下一处改动' }).click();
});

test('terminal tabs use the terminal icon', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '终端', exact: true }).click();

  await expect(page.locator('.agent-workspace-tabs > .active .lucide-square-terminal')).toBeVisible();
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
  await dragToolTo(page, (await splitGeometry(page)).width + 130);
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
  await page.evaluate(() => localStorage.setItem('flowweave:environment-card-open', 'false'));
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

for (const interaction of ['pointer', 'keyboard'] as const) {
  test(`message scale previews cached history before rendering and locates it by formal identity (${interaction})`, async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const latest = [
      { id: 'scale-latest-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '当前提问', timestamp: now } },
      { id: 'scale-latest-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'scale-latest-user', content: '当前已渲染的最新回复需要保持足够滚动高度。 '.repeat(1_000), final_reply: true, timestamp: now } },
    ];
    const message = (id: string, content: string, extra: Record<string, unknown> = {}) => ({
      id, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content, timestamp: now, ...extra },
    });
    const displayMessage = message('scale-history-display', '不应展示的原始上下文', { display_content: '历史展示正文' });
    const oldest = message('scale-history-oldest', '最早的真实提问\n保留第二行内容', { source: 'human' });
    const contextMessage = message('scale-history-context', `上下文前言\n---FLOWWEAVE_MESSAGE_CONTEXT_V5---\n${JSON.stringify({ current_user_request: { content: '带引用的真实历史提问' }, reference_materials: [{ content: '不应显示的引用原文' }] })}`);
    const emptyMessage = message('scale-history-empty', '', { attachments: [] });
    const other = [message('scale-other-user', '另一个会话的提问')];
    let historyReads = 0;
    let latestReads = 0;
    await openWorkspace(page, latest, true);
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations/*/hydration', route => {
      const isOther = new URL(route.request().url()).pathname.includes('/scheduled-conversation-2/');
      return json(route, {
        events: { events: isOther ? other : latest, next_cursor: null, history_cursor: isOther ? null : 'scale-bridge', result: { status: 'COMPLETED' } },
        readiness: { ready: true, execution_status: 'idle' }, context: {},
      });
    });
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations/*/events*', route => {
      const url = new URL(route.request().url());
      if (url.pathname.includes('/scheduled-conversation-2/')) return json(route, { events: other, history_cursor: null, next_cursor: null });
      const cursor = url.searchParams.get('history_cursor');
      if (cursor === 'scale-bridge') return json(route, { events: [], history_cursor: 'scale-page-1', next_cursor: null });
      if (cursor) historyReads += 1;
      else latestReads += 1;
      if (cursor === 'scale-page-1') return json(route, { events: [displayMessage, ...latest], history_cursor: 'scale-page-2', next_cursor: null });
      if (cursor === 'scale-page-2') return json(route, {
        events: [oldest, contextMessage, emptyMessage, message('scale-protocol', '[FLOWWEAVE_INTERNAL]\n内部协议'), displayMessage],
        history_cursor: null, next_cursor: null,
      });
      return json(route, { events: latest, history_cursor: 'scale-page-1', next_cursor: null });
    });
    await page.goto('/agent/conversations/scheduled-conversation');
    const surface = page.locator('.conversation-surface');
    const scale = page.getByRole('navigation', { name: '用户消息导航' });
    const tooltip = page.getByRole('tooltip');
    await expect.poll(() => historyReads).toBe(2);
    // Older cached messages are still absent from the transcript DOM, but
    // must already have distinct, accurate navigation previews.
    await expect(surface.locator('[data-user-event-id="scale-history-oldest"]')).toHaveCount(0);
    await expect(scale.getByRole('button')).toHaveCount(5);
    expect(await scale.getByRole('button').evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')))).toEqual([
      '定位到用户消息：最早的真实提问 保留第二行内容',
      '定位到用户消息：带引用的真实历史提问',
      '定位到用户消息：空消息',
      '定位到用户消息：历史展示正文',
      '定位到用户消息：当前提问',
    ]);
    const requestsBeforePreview = { historyReads, latestReads };
    const scrollBeforePreview = await surface.evaluate(element => element.scrollTop);
    for (const [label, content] of [
      ['最早的真实提问 保留第二行内容', oldest.payload.content],
      ['带引用的真实历史提问', '带引用的真实历史提问'],
      ['空消息', '（空消息）'],
      ['历史展示正文', '历史展示正文'],
    ]) {
      const tick = scale.getByRole('button', { name: `定位到用户消息：${label}`, exact: true });
      if (interaction === 'pointer') await tick.hover();
      else await tick.focus();
      await expect(tooltip).toHaveText(content);
      await expect(surface.locator('[data-user-event-id="scale-history-oldest"]')).toHaveCount(0);
    }
    expect({ historyReads, latestReads }).toEqual(requestsBeforePreview);
    expect(await surface.evaluate(element => element.scrollTop)).toBe(scrollBeforePreview);
    if (interaction === 'pointer') await page.screenshot({ path: '../../.tmp/fr639-cached-message-preview.png' });
    const target = scale.getByRole('button', { name: '定位到用户消息：最早的真实提问 保留第二行内容', exact: true });
    if (interaction === 'pointer') await target.click();
    else { await target.focus(); await target.press('Enter'); }
    const revealed = surface.locator('[data-user-event-id="scale-history-oldest"]');
    await expect(revealed).toBeInViewport();
    await expect(scale.getByRole('button')).toHaveCount(5);
    await expect(surface.locator('[data-user-event-id="scale-history-display"]')).toHaveCount(1);
    if (interaction === 'pointer') await target.hover();
    else await target.focus();
    await expect(tooltip).toHaveText(oldest.payload.content);
    expect({ historyReads, latestReads }).toEqual(requestsBeforePreview);
    await page.getByRole('button', { name: '周报会话', exact: true }).click();
    await expect(scale.getByRole('button')).toHaveCount(1);
    await expect(scale.getByRole('button')).toHaveAccessibleName('定位到用户消息：另一个会话的提问');
    await expect(tooltip).toHaveCount(0);
    await expect(surface).not.toContainText('最早的真实提问');
  });
}

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


test('isolated workspace browser renders real Vite modules through a deployment prefix', async ({ page, context }) => {
  const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
  await mkdir(path.join(repository, '.tmp'), {recursive:true});
  const scratch = await mkdtemp(path.join(repository, '.tmp/browser-vite-'));
  const freePort = async () => {
    const server = createServer();
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('No test port');
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    return address.port;
  };
  const vitePort = await freePort();
  const apiPort = await freePort();
  await mkdir(path.join(scratch, 'src'));
  await writeFile(path.join(scratch, 'index.html'), '<!doctype html><HEAD><title>Vite preview</title></HEAD><body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body>');
  await writeFile(path.join(scratch, 'src/main.jsx'), `import React from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { message } from './message.js';
createRoot(document.getElementById('root')).render(<h1 id="preview-ready">{message}</h1>);
import('./lazy.js').then(({value})=>document.body.dataset.lazy=value);
fetch('/data.json',{credentials:'include',headers:{'Content-Type':'application/json'}}).then(r=>r.json()).then(data=>document.body.dataset.fetched=data.value);
localStorage.setItem('preview-key','isolated');
sessionStorage.setItem('preview-key','temporary');
document.body.dataset.storage=localStorage.getItem('preview-key')+'/'+sessionStorage.getItem('preview-key');
const xhr=new XMLHttpRequest();xhr.open('GET','/data.json');xhr.withCredentials=true;xhr.setRequestHeader('Content-Type','application/json');xhr.onload=()=>document.body.dataset.xhr=JSON.parse(xhr.responseText).value;xhr.send();
try { window.parent.document.body.dataset.escaped='yes'; } catch { document.body.dataset.isolated='yes'; }
`);
  await writeFile(path.join(scratch, 'src/message.js'), `export {message} from './nested.js';`);
  await writeFile(path.join(scratch, 'src/nested.js'), `export const message='Vite 模块加载成功';`);
  // Large development modules (including inline maps) must survive every
  // transport layer; the old 2 MiB cap passed the previous tiny lazy fixture.
  await writeFile(path.join(scratch, 'src/lazy.js'), `export const value='loaded';\n/*${'x'.repeat(3 * 1024 * 1024)}*/`);
  await writeFile(path.join(scratch, 'src/style.css'), `@import './theme.css'; #preview-ready{color:rgb(12, 34, 56);background-image:url('../pixel.svg')}`);
  await writeFile(path.join(scratch, 'src/theme.css'), `body{margin:13px}`);
  await writeFile(path.join(scratch, 'pixel.svg'), '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><!--'+'x'.repeat(5000)+'--></svg>');
  await writeFile(path.join(scratch, 'data.json'), JSON.stringify({value:'runtime'}));
  const config = path.join(scratch, 'vite.config.mjs');
  await writeFile(config, `import {defineConfig} from ${JSON.stringify(path.join(repository, 'apps/web/node_modules/vite/dist/node/index.js'))};
import react from ${JSON.stringify(path.join(repository, 'apps/web/node_modules/@vitejs/plugin-react/dist/index.mjs'))};
export default defineConfig({root:${JSON.stringify(scratch)}, plugins:[react()], resolve:{alias:{react:${JSON.stringify(path.join(repository, 'apps/web/node_modules/react'))},'react-dom':${JSON.stringify(path.join(repository, 'apps/web/node_modules/react-dom'))}}},server:{host:'127.0.0.1',port:${vitePort},strictPort:true}});`);
  const vite = spawn(process.execPath, [path.join(repository, 'apps/web/node_modules/vite/bin/vite.js'), '--config', config, '--configLoader', 'native'], {cwd:repository,stdio:'pipe'});
  const docker = path.join(scratch, 'docker');
  const python = path.join(repository, 'services/platform/.venv/bin/python');
  await writeFile(docker, `#!${python}\nimport os,sys\nassert sys.argv[1:3]==['exec','-i']\nos.execv(sys.executable,[sys.executable,*sys.argv[5:]])\n`);
  await chmod(docker, 0o700);
  const apiServer = spawn(path.join(repository, 'services/platform/.venv/bin/python'), [path.join(repository, 'apps/web/e2e/fixtures/browser_preview_server.py')], {
    cwd:repository, stdio:'pipe', env:{...process.env, PYTHONPATH:path.join(repository,'services/platform/src'), PREVIEW_VITE_PORT:String(vitePort), PREVIEW_API_PORT:String(apiPort), PREVIEW_DOCKER_BINARY:docker},
  });
  let diagnostics = '';
  for (const child of [vite, apiServer]) {
    child.stdout?.on('data', data => { diagnostics += data.toString(); });
    child.stderr?.on('data', data => { diagnostics += data.toString(); });
  }
  const origin = `http://127.0.0.1:${apiPort}`;
  try {
    await expect.poll(async () => {
      try { return (await fetch(`http://127.0.0.1:${vitePort}/`)).status; } catch { return 0; }
    }, {message:'Vite starts',timeout:30_000}).toBe(200);
    await expect.poll(async () => {
      try { return (await fetch(origin+'/host')).status; } catch { return 0; }
    }, {message:'Preview API starts',timeout:30_000}).toBe(200);
    const largeModule = await fetch(`http://127.0.0.1:${vitePort}/src/lazy.js`);
    expect((await largeModule.arrayBuffer()).byteLength).toBeGreaterThan(2 * 1024 * 1024);
    await openWorkspace(page, [
      {id:'preview-user',event_type:'MESSAGE',payload:{source:'user',parent_id:'__root__',content:'预览页面',timestamp:now}},
      {id:'preview-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'preview-user',content:`[本地预览](http://localhost:${vitePort}/)`,timestamp:now}},
    ]);
    await context.addCookies([{name:'flowweave_session',value:'isolated-browser-preview-test-session',url:origin,sameSite:'Lax'}]);
    await page.route(`**/api/v1/agent-workspaces/schedule-workspace/browser/http/${vitePort}/**`, async route => {
      const url = new URL(route.request().url());
      url.searchParams.set('deployment_base_path','/flowweave');
      const response = await context.request.get(origin+'/flowweave'+url.pathname+url.search);
      // The test API is a separate local origin; production shares the host.
      const body = (await response.text()).replaceAll('"/flowweave/api/', `"${origin}/flowweave/api/`);
      await route.fulfill({response,body});
    });
    // Fixture requests bypass the product API mocks installed by openWorkspace.
    await page.route(origin+'/**', route => route.continue());
    const resources: {path:string;status:number}[] = [];
    page.on('response', response=>{
      if(response.url().startsWith(origin))resources.push({path:decodeURI(new URL(response.url()).pathname),status:response.status()});
    });
    await page.goto('/agent/conversations/scheduled-conversation');
    await page.getByRole('link',{name:'本地预览',exact:true}).click();
    const iframe = page.locator('.agent-workspace-browser-tab-panel.active iframe');
    const frame = iframe.contentFrame();
    await expect(frame.getByRole('heading',{name:'Vite 模块加载成功'})).toBeVisible();
    await expect(frame.locator('body')).toHaveAttribute('data-lazy','loaded');
    await expect(frame.locator('body')).toHaveAttribute('data-fetched','runtime');
    await expect(frame.locator('body')).toHaveAttribute('data-xhr','runtime');
    await expect(frame.locator('body')).toHaveAttribute('data-isolated','yes');
    await expect(frame.locator('body')).toHaveAttribute('data-storage','isolated/temporary');
    expect(await page.evaluate(()=>localStorage.getItem('preview-key'))).toBeNull();
    await expect(frame.locator('#preview-ready')).toHaveCSS('color','rgb(12, 34, 56)');
    await expect(frame.locator('body')).toHaveCSS('margin','13px');
    expect(await page.locator('body').getAttribute('data-escaped')).toBeNull();
    expect(await iframe.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(await frame.locator('body').evaluate(()=>window.origin)).toBe('null');
    expect(resources.filter(resource=>resource.status>=400)).toEqual([]);
    expect(resources.every(resource=>resource.path.startsWith('/flowweave/api/v1/agent-workspaces/'))).toBe(true);
    expect(resources.some(resource=>resource.path.endsWith('/@react-refresh')), 'Vite React preamble loads').toBe(true);
    await expect.poll(()=>resources.some(resource=>resource.path.endsWith('/pixel.svg')), {message:'CSS background image loads through the proxy'}).toBe(true);
    await page.screenshot({path:path.join(scratch,'preview.png')});
  } catch (error) {
    // Redact scoped grant URLs from fixture diagnostics as well.
    throw new Error(String(error)+'\n'+diagnostics.replace(/fwpreview_[A-Za-z0-9_=-]+/g,'[preview-grant]'));
  } finally {
    for (const child of [vite,apiServer]) child.kill('SIGTERM');
    await Promise.all([vite,apiServer].map(child=>new Promise<void>(resolve=>{
      if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve());
    })));
  }
});


test('Runtime browser loading covers delayed module rendering and rejects unrelated readiness messages', async ({ page }) => {
  await openWorkspace(page, [
    { id:'loading-user',event_type:'MESSAGE',payload:{source:'user',parent_id:'__root__',content:'预览',timestamp:now} },
    { id:'loading-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'loading-user',content:'[延迟预览](http://localhost:3000/)',timestamp:now} },
  ]);
  let release:()=>void=()=>undefined;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/browser/http/3000/**',route=>route.fulfill({contentType:'text/html',body:'<html><head></head><body><div id="root"></div><script type="module" src="/preview-delayed-entry.js"></script></body></html>'}));
  await page.route('**/preview-delayed-entry.js',async route=>{
    await gate;
    await route.fulfill({contentType:'text/javascript',headers:{'Access-Control-Allow-Origin':'*'},body:'document.getElementById("root").innerHTML="<h1>异步页面已显示</h1>";'});
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link',{name:'延迟预览',exact:true}).click();
  const panel=page.locator('.agent-workspace-browser-tab-panel.active');
  const loading=panel.getByRole('status');
  await expect(loading).toContainText('正在加载页面资源');
  const iframe=panel.locator('iframe');
  await iframe.evaluate(element=>{
    const iframe=element as HTMLIFrameElement;
    const id=iframe.srcdoc.match(/const id="([^"]+)"/)?.[1];
    window.dispatchEvent(new MessageEvent('message',{origin:'null',source:window,data:{kind:'flowweave-browser-preview-load',id,phase:'ready'}}));
    window.dispatchEvent(new MessageEvent('message',{origin:'null',source:iframe.contentWindow,data:{kind:'flowweave-browser-preview-load',id:'wrong-load',phase:'ready'}}));
  });
  await expect(loading).toBeVisible();
  await page.screenshot({path:path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../.tmp/fr631-loading.png')});
  release();
  await expect(iframe.contentFrame().getByRole('heading',{name:'异步页面已显示'})).toBeVisible();
  await expect(loading).toBeHidden();
  await expect(panel.locator('.agent-workspace-browser-content')).toHaveAttribute('aria-busy','false');
});


test('Runtime browser shows script failure and reloads with a fresh frame', async ({ page }) => {
  await openWorkspace(page,[{id:'broken-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'__root__',content:'[故障预览](http://localhost:3000/)',timestamp:now}}]);
  let reads=0;
  let failStartup=true;
  await page.route('**/browser/http/3000/**',route=>{
    reads++;
    return route.fulfill({contentType:'text/html',body:failStartup?'<html><head></head><body><div id="root"></div><script>throw new Error("broken startup")</script></body></html>':'<html><body><h1>重新加载成功</h1></body></html>'});
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link',{name:'故障预览',exact:true}).click();
  const panel=page.locator('.agent-workspace-browser-tab-panel.active');
  await expect(panel.getByRole('alert')).toContainText('页面加载失败');
  failStartup=false;
  await panel.getByRole('button',{name:'重新加载页面',exact:true}).click();
  await expect(panel.locator('iframe').contentFrame().getByRole('heading',{name:'重新加载成功'})).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  expect(reads).toBeGreaterThanOrEqual(2);
});


test('Runtime browser slow loading remains recoverable when the delayed app finally renders', async ({ page }) => {
  await page.clock.install();
  await openWorkspace(page,[{id:'slow-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'__root__',content:'[缓慢预览](http://localhost:3000/)',timestamp:now}}]);
  let release:()=>void=()=>undefined;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/browser/http/3000/**',route=>route.fulfill({contentType:'text/html',body:'<html><head></head><body><div id="root"></div><script type="module" src="/preview-slow-entry.js"></script></body></html>'}));
  await page.route('**/preview-slow-entry.js',async route=>{
    await gate;
    await route.fulfill({contentType:'text/javascript',headers:{'Access-Control-Allow-Origin':'*'},body:'document.getElementById("root").innerHTML="<h1>缓慢页面已恢复</h1>";'});
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link',{name:'缓慢预览',exact:true}).click();
  const panel=page.locator('.agent-workspace-browser-tab-panel.active');
  await expect(panel.getByRole('status')).toContainText('正在加载页面资源');
  await page.clock.fastForward(25_001);
  await expect(panel.getByRole('alert')).toContainText('页面加载较慢');
  release();
  await page.clock.resume();
  await expect(panel.locator('iframe').contentFrame().getByRole('heading',{name:'缓慢页面已恢复'})).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
});


test('Runtime browser ignores an outgoing session response after switching bindings', async ({ page }) => {
  const user={id:'switch-user',event_type:'MESSAGE',payload:{source:'user',parent_id:'__root__',content:'切换预览',timestamp:now}};
  const reply={id:'switch-agent',event_type:'MESSAGE',payload:{source:'agent',parent_id:'switch-user',content:'[会话预览](http://localhost:3000/)',timestamp:now}};
  await openWorkspace(page,[user,reply],true);
  let release:()=>void=()=>undefined;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  let oldStarted=false;
  await page.route('**/browser/http/3000/**',async route=>{
    const binding=new URL(route.request().url()).searchParams.get('binding_id');
    if(binding==='scheduled-conversation'){
      oldStarted=true;
      await gate;
    }
    await route.fulfill({contentType:'text/html',body:`<html><body><h1>${binding==='scheduled-conversation'?'旧会话内容':'新会话内容'}</h1></body></html>`}).catch(()=>undefined);
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('link',{name:'会话预览',exact:true}).click();
  await expect.poll(()=>oldStarted).toBe(true);
  await page.getByRole('button',{name:'周报会话',exact:true}).dblclick();
  await page.getByRole('link',{name:'会话预览',exact:true}).click();
  const iframe=page.locator('.agent-workspace-browser-tab-panel.active iframe');
  await expect(iframe.contentFrame().getByRole('heading',{name:'新会话内容'})).toBeVisible();
  release();
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(iframe.contentFrame().getByRole('heading',{name:'新会话内容'})).toBeVisible();
  await expect(iframe.contentFrame().getByRole('heading',{name:'旧会话内容'})).toHaveCount(0);
});

test('schedule configuration aligns its external label and control with the form fields', async ({ page }) => {
  await openWorkspace(page);
  await page.goto('/agent/schedules');
  await page.locator('.agent-schedule-tree-select').first().click();
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 800 }, { width: 640, height: 900 }]) {
    await page.setViewportSize(viewport);
    const config = page.locator('.agent-schedule-session-config');
    await expect(config).toBeVisible();
    expect(await config.evaluate(element => {
      const field = element.getBoundingClientRect();
      const effort = document.querySelector<HTMLButtonElement>('[aria-label="思考程度"]')!.getBoundingClientRect();
      const title = element.parentElement!.firstChild!.textContent;
      return { height: field.height, effortHeight: effort.height, aligned: innerWidth <= 760 || field.top === effort.top, title, overflow: element.scrollWidth > element.clientWidth };
    })).toEqual({ height: 56, effortHeight: 56, aligned: true, title: '会话配置', overflow: false });
    await page.screenshot({ path: `../../.tmp/fr653-form-${viewport.width}.png` });
  }
});

test('schedule runs deduplicate continued conversations and share selection and native status indicators', async ({ page }) => {
  await openWorkspace(page);
  const titles = ['执行中', '普通未读', '系统异常', '手工未读', '运行异常'];
  const items = titles.map((display_title, index) => ({ id: `scheduled-run-${index + 1}`, display_title, lifecycle: 'ACTIVE', streaming_callback_ready: true, created_at: now, updated_at: now, unread: index > 0, unread_origin: index === 2 || index === 4 ? 'SYSTEM' : 'MANUAL' }));
  await page.route('**/conversation-schedules/*/occurrences', route => json(route, [
    { id: 'pending-duplicate', binding_id: items[0].id, state: 'PENDING', created_at: now },
    ...items.map((conversation, index) => ({ id: `occurrence-${index}`, binding_id: conversation.id, conversation_title: conversation.display_title, conversation, state: 'STARTED', trigger_kind: 'SCHEDULED', created_at: now })),
    { id: 'old-duplicate', binding_id: items[0].id, conversation: items[0], state: 'STARTED', created_at: now },
  ]));
  await page.route('**/conversation-activity*', route => json(route, { conversations: items, running_binding_ids: [items[0].id, items[4].id], failed_binding_ids: [items[2].id, items[3].id], possibly_stuck_binding_ids: [items[0].id, items[4].id] }));
  await page.goto('/agent/schedules');
  const group = page.locator('.agent-schedule-tree-group').first();
  await group.locator('.agent-schedule-tree-select').click();
  await expect(page.locator('.agent-schedule-history-run')).toHaveCount(5);
  await group.locator('.agent-schedule-tree-toggle').click();
  await group.getByRole('button', { name: '展开其余 2 个会话' }).click();
  await expect(group.locator('.agent-schedule-run')).toHaveCount(5);
  await expect(group.getByRole('img', { name: '会话正在运行', exact: true })).toHaveCount(1);
  await expect(group.getByRole('img', { name: '会话已完成，有未读回复' })).toHaveCount(2);
  await expect(group.getByRole('button', { name: '确认会话异常已读' })).toHaveCount(1);
  await expect(group.locator('.agent-workspace-conversation-alert.running')).toHaveCount(1);
  await expect(group.locator('em')).toHaveCount(0);
  await group.getByRole('button', { name: '执行中', exact: true }).click();
  await expect(group.locator('[data-binding-id="scheduled-run-1"]')).toHaveClass(/ active/);
  await expect(group.locator('[data-binding-id="scheduled-run-2"]')).not.toHaveClass(/ active/);
  await page.screenshot({ path: '../../.tmp/fr653-schedule-states.png' });
});

test('scheduled conversation API file links preview and download with the active binding', async ({ page }) => {
  const root = '/runtime/workspace/project/users/test-user/.flowweave/automations/daily';
  const report = `${root}/report.xlsx`;
  const origin = new URL(test.info().project.use.baseURL ?? 'http://127.0.0.1:5173').origin;
  const apiLink = `${origin}/api/v1/agent-workspaces/schedule-workspace/workspace/file?path=${encodeURIComponent(report)}`;
  await openWorkspace(page, [
    { id: 'file-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '导出表格', timestamp: now } },
    { id: 'file-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'file-user', content: `[下载 Excel 明细表](${apiLink})`, timestamp: now } },
  ]);
  await page.route('**/api/v1/agent-workspaces/*/conversations/scheduled-run-1', route => json(route, { id: 'scheduled-run-1', display_title: '定时报告', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, working_directory: root, created_at: now, updated_at: now }));
  await page.route('**/schedule-workspace/workspace?**', route => json(route, { root, working_directory: root, scope: { kind: 'WORK_DIRECTORY', display_name: '定时报告' }, files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } }));
  let downloaded = false;
  await page.route('**/schedule-workspace/workspace/file?**', async route => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get('path')).toBe(report);
    expect(url.searchParams.get('binding_id')).toBe('scheduled-run-1');
    expect(url.searchParams.get('download')).toBe('true');
    downloaded = true;
    await route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers: { 'Content-Disposition': 'attachment; filename="report.xlsx"' }, body: 'xlsx-fixture' });
  });
  await page.goto('/agent/schedules/conversations/scheduled-run-1');
  await page.getByRole('link', { name: '下载 Excel 明细表' }).click();
  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview).toBeVisible();
  expect(downloaded).toBe(false);
  const downloadEvent = page.waitForEvent('download');
  await preview.getByRole('link', { name: '下载', exact: true }).click();
  expect((await downloadEvent).suggestedFilename()).toBe('report.xlsx');
  expect(downloaded).toBe(true);
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
});

test('scheduled Chinese text file links keep the complete path for preview and download', async ({ page }) => {
  const root = '/runtime/workspace/project/users/00000000-0000-0000-0000-000000000001/.flowweave/automations/00000000-0000-0000-0000-000000000002';
  const filename = '2026-10-23_美股估值增量任务受影响证券摘要.txt';
  const report = `${root}/${filename}`;
  const content = '受影响证券文本摘要\n这是用于验证中文文件预览和下载的测试文本。\n';
  const query = new URLSearchParams({ path: report, binding_id: 'scheduled-run-1' });
  const apiLink = `/api/v1/agent-workspaces/schedule-workspace/workspace/file?${query}`;
  await openWorkspace(page, [
    { id: 'file-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '导出文本', timestamp: now } },
    { id: 'file-agent', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'file-user', content: `[下载受影响证券文本摘要](${apiLink})`, timestamp: now } },
  ]);
  await page.route('**/api/v1/agent-workspaces/*/conversations/scheduled-run-1', route => json(route, { id: 'scheduled-run-1', display_title: '定时报告', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, working_directory: root, created_at: now, updated_at: now }));
  await page.route('**/schedule-workspace/workspace?**', route => json(route, { root, working_directory: root, scope: { kind: 'WORK_DIRECTORY', display_name: '定时报告' }, files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } }));
  const reads: string[] = [];
  await page.route('**/schedule-workspace/workspace/file?**', async route => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get('path')).toBe(report);
    expect(url.searchParams.get('binding_id')).toBe('scheduled-run-1');
    const download = url.searchParams.get('download') === 'true';
    reads.push(download ? 'download' : 'preview');
    if (!download) expect(url.searchParams.get('preview')).toBe('true');
    await route.fulfill({
      contentType: 'text/plain; charset=utf-8',
      headers: {
        'Content-Disposition': `${download ? 'attachment' : 'inline'}; filename*=UTF-8''${encodeURIComponent(filename)}`,
        'X-Preview-Total-Bytes': String(Buffer.byteLength(content)),
      },
      body: content,
    });
  });
  await page.goto('/agent/schedules/conversations/scheduled-run-1');
  await page.getByRole('link', { name: '下载受影响证券文本摘要' }).click();
  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview.getByRole('heading', { name: filename, exact: true })).toBeVisible();
  await expect(preview.locator('.agent-file-preview-dialog-content')).toContainText('这是用于验证中文文件预览和下载的测试文本。');
  expect(reads).toContain('preview');
  expect(reads).not.toContain('download');
  await page.screenshot({ path: '../../.tmp/fr654-chinese-text-preview.png' });
  const downloadEvent = page.waitForEvent('download');
  await preview.getByRole('link', { name: '下载', exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe(filename);
  const output = path.resolve('../../.tmp/fr654-text-download.txt');
  await download.saveAs(output);
  expect(await readFile(output, 'utf8')).toBe(content);
  expect(reads.filter(read => read === 'download')).toHaveLength(1);
  await expect(page).toHaveURL(/\/agent\/schedules\/conversations\/scheduled-run-1$/);
});


for (const config of ['untouched', 'capabilities', 'deselect'] as const) {
  test(`new schedule defaults all credentials and preserves ${config} configuration`, async ({ page }) => {
    const writes = await openWorkspace(page);
    await page.route('**/website-credentials', route => json(route, [1, 2].map(n => ({ id: `credential-${n}`, name: `认证${n}`, target_host: 'example.test', target_path: '/', auth_type: 'TOKEN' }))));
    await page.getByRole('navigation', { name: 'Agent 工作区导航' }).getByRole('button', { name: '定时任务' }).click();
    await page.getByRole('region', { name: '会话定时任务' }).getByRole('button', { name: '新建', exact: true }).click();
    await page.getByLabel('任务名称', { exact: true }).fill('默认认证');
    await page.getByLabel('业务提示词').fill('验证默认认证');
    if (config !== 'untouched') {
      await page.getByRole('button', { name: /配置会话/ }).click();
      const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
      await expect(dialog.getByText('已选择 1 项', { exact: true })).toBeVisible();
      if (config === 'capabilities') {
        await dialog.getByRole('button', { name: /日报技能/ }).click();
        await dialog.getByRole('button', { name: '保存会话配置', exact: true }).click();
      } else {
        await dialog.getByRole('button', { name: '认证', exact: true }).click();
        await expect(dialog.getByText('已选择 2 项认证', { exact: true })).toBeVisible();
        await dialog.getByRole('button', { name: '取消全选认证', exact: true }).click();
        await dialog.getByRole('button', { name: '保存认证配置', exact: true }).click();
        await page.getByRole('button', { name: /配置会话/ }).click();
        await dialog.getByRole('button', { name: '认证', exact: true }).click();
        await expect(dialog.getByText('已选择 0 项认证', { exact: true })).toBeVisible();
        await dialog.getByRole('button', { name: '保存认证配置', exact: true }).click();
      }
    }
    await page.getByRole('button', { name: '保存任务', exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].body.credential_ids).toEqual(config === 'deselect' ? [] : ['credential-1', 'credential-2']);
    expect(writes[0].body.capability_version_ids).toEqual(config === 'capabilities' ? ['capability-1'] : []);
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('button', { name: /配置会话/ }).click();
    const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
    await dialog.getByRole('button', { name: '认证', exact: true }).click();
    await expect(dialog.getByText(`已选择 ${config === 'deselect' ? 0 : 2} 项认证`, { exact: true })).toBeVisible();
  });
}

for (const unavailable of [false, true]) {
  test(`whole conversation drop sends only binding metadata and ${unavailable ? 'preserves draft on capability failure' : 'keeps its source link'}`, async ({ page }) => {
    await openWorkspace(page);
    await page.goto('/agent/conversations/scheduled-conversation');
    const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
    await expect(input).toBeEnabled();
    const requests: Record<string, unknown>[] = [];
    await page.route('**/conversations/scheduled-conversation/messages', route => {
      requests.push(route.request().postDataJSON());
      return unavailable
        ? json(route, { error: { code: 'AGENT_HISTORY_CAPABILITY_UNAVAILABLE', message: '会话历史查询能力不可用，请检查平台配置后重试；引用和草稿已保留' } }, 409)
        : json(route, { accepted: true, cursor: 'metadata-user-event' }, 202);
    });
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData('application/x-flowweave-conversation-reference', JSON.stringify({ bindingId: 'reference-source', eventId: 'sidebar-leaf-is-not-a-snippet', title: '引用来源会话' }));
      return data;
    });
    await page.locator('.agent-workbench-content').dispatchEvent('drop', { dataTransfer: transfer });
    await input.fill('根据来源继续当前任务');
    await input.press('Enter');
    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0].references).toEqual([{ source_binding_id: 'reference-source' }]);
    expect(JSON.stringify(requests[0])).not.toContain('sidebar-leaf-is-not-a-snippet');
    if (unavailable) {
      await expect(page.getByText('会话历史查询能力不可用，请检查平台配置后重试；引用和草稿已保留', { exact: true })).toBeVisible();
      await expect(input).toHaveValue('根据来源继续当前任务');
      await expect(page.getByRole('button', { name: '移除会话 引用来源会话', exact: true })).toBeVisible();
    } else {
      await expect(page.locator('.conversation-message.user').getByRole('button', { name: /打开会话/ })).toBeVisible();
    }
  });
}

for (const width of [1440, 1024]) {
  test(`conversation error notice is compact and dismissible at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await openWorkspace(page);
    const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
    await expect(input).toBeEnabled();
    await page.route('**/agent-workspaces/schedule-workspace/conversations', route => route.request().method() === 'POST'
      ? json(route, { error: { code: 'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS', message: '首条消息待核对' } }, 504)
      : route.fallback());
    await input.fill('保留这条草稿');
    await input.press('Enter');
    const notice = page.getByRole('alert');
    await expect(notice).toContainText('首条消息核对超时');
    await expect(notice).not.toContainText('操作未完成');
    const box = await notice.boundingBox();
    expect(box!.height).toBeLessThanOrEqual(42);
    expect(box!.width).toBeLessThanOrEqual(720);
    expect(await notice.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: path.resolve('../../.tmp/fr670-error-notice', `notice-${width}.png`) });
    await notice.getByRole('button', { name: '关闭错误提示' }).click();
    await expect(notice).toHaveCount(0);
    await expect(input).toHaveValue('保留这条草稿');
    await expect(input).toBeEnabled();
  });
}

test('conversation error notice expires safely and resets on retry and conversation switch', async ({ page }) => {
  await openWorkspace(page, [], true);
  await page.goto('/agent/conversations/scheduled-conversation');
  const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
  await expect(input).toBeEnabled();
  await page.clock.install();
  let requests = 0;
  await page.route('**/conversations/scheduled-conversation/messages', route => {
    requests += 1;
    return json(route, { error: { code: 'AGENT_HISTORY_CAPABILITY_UNAVAILABLE', message: '发送暂未完成，草稿已保留。' } }, 409);
  });
  await input.fill('自动收起不应清空草稿');
  await input.press('Enter');
  const notice = page.getByRole('alert');
  await expect(notice).toBeVisible();
  await page.clock.fastForward(4_000);
  await input.press('Enter');
  await expect.poll(() => requests).toBe(2);
  await expect(notice).toBeVisible();
  await page.clock.fastForward(4_500);
  await expect(notice).toBeVisible();
  await notice.hover();
  await page.clock.fastForward(9_000);
  await expect(notice).toBeVisible();
  await notice.getByRole('button', { name: '关闭错误提示' }).focus();
  await page.mouse.move(0, 0);
  await page.clock.fastForward(9_000);
  await expect(notice).toBeVisible();
  await input.focus();
  await page.clock.fastForward(8_100);
  await expect(notice).toHaveCount(0);
  await expect(input).toHaveValue('自动收起不应清空草稿');
  await input.press('Enter');
  await expect(notice).toBeVisible();
  await page.getByRole('button', { name: '周报会话', exact: true }).dblclick();
  await expect(page).toHaveURL(/scheduled-conversation-2$/);
  await expect(input).toBeEnabled();
  await expect(notice).toHaveCount(0);
  await input.fill('新会话独立草稿');
  await page.clock.fastForward(8_100);
  await expect(input).toHaveValue('新会话独立草稿');
});

test('late history capability failure restores only its outgoing conversation draft', async ({ page }) => {
  await openWorkspace(page, [], true);
  await page.goto('/agent/conversations/scheduled-conversation');
  const input = page.getByRole('textbox', { name: '发送 Agent 消息', exact: true });
  await expect(input).toBeEnabled();
  let release: (() => void) | undefined;
  let requested = false;
  await page.route('**/conversations/scheduled-conversation/messages', async route => {
    requested = true;
    await new Promise<void>(resolve => { release = resolve; });
    await json(route, { error: { code: 'AGENT_HISTORY_CAPABILITY_UNAVAILABLE', message: '来源查询暂不可用' } }, 409);
  });
  const transfer = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.setData('application/x-flowweave-conversation-reference', JSON.stringify({ bindingId: 'reference-source', eventId: '', title: '来源' }));
    return data;
  });
  await page.locator('.agent-workbench-content').dispatchEvent('drop', { dataTransfer: transfer });
  await input.fill('原会话失败后应恢复的内容');
  await input.press('Enter');
  await expect.poll(() => requested).toBe(true);
  await page.getByRole('button', { name: '周报会话', exact: true }).dblclick();
  await expect(page).toHaveURL(/scheduled-conversation-2$/);
  await expect(input).toBeEnabled();
  await input.fill('新会话独立草稿');
  release?.();
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage).some(key => key.includes('scheduled-conversation') && (localStorage.getItem(key) ?? '').includes('原会话失败后应恢复的内容')))).toBe(true);
  await expect(input).toHaveValue('新会话独立草稿');
  await expect(page.getByText('来源查询暂不可用', { exact: true })).toHaveCount(0);
});

test('unified case material import keeps edits through failure and cancels only its new reference', async ({ page }) => {
  await openWorkspace(page);
  let item = { id: 'import-case', name: '资料场景', description: '原有说明', objective: '原有目标', work_directory_id: null, state: 'DRAFT', row_version: 4,
    materials: [{ kind: 'LINK', name: '已保存资料', value: 'https://example.test/original' }], stages: [], tasks: [], control_plans: [], decision_proposals: [], created_at: now, updated_at: now };
  let uploads = 0;
  const writes: Record<string, unknown>[] = [];
  await page.route('**/api/v1/cases**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'GET') return json(route, [item]);
    if (pathname.endsWith('/materials')) {
      expect(new URL(request.url()).searchParams.get('expected_row_version')).toBe(String(item.row_version));
      uploads += 1;
      if (uploads === 1) return json(route, { error: { code: 'UPLOAD_FAILED', message: '上传暂不可用' } }, 503);
      item = { ...item, row_version: item.row_version + 1, materials: [...item.materials, { kind: 'FILE', name: 'input.txt', value: '.flowweave/cases/import-case/materials/imported-file' }] };
      return json(route, item, 201);
    }
    const body = request.postDataJSON();
    expect(body.expected_row_version).toBe(item.row_version);
    writes.push(body);
    item = { ...item, ...body, row_version: item.row_version + 1 };
    return json(route, item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await openCasePanel(page, '场景信息');
  await expect(page.getByRole('textbox', { name: '说明', exact: true })).toHaveAttribute('readonly');
  await page.getByRole('button', { name: '添加资料', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '添加资料', exact: true });
  await expect(page.getByRole('button', { name: '添加链接', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '添加文件', exact: true })).toHaveCount(0);
  await modal.getByLabel('资料名称').fill('导入资料.txt');
  await modal.getByLabel('资料地址').fill('intranet/manual');
  const file = { name: 'input.txt', mimeType: 'text/plain', buffer: Buffer.from('inputs') };
  await modal.getByLabel('导入资料文件').setInputFiles(file);
  await expect(modal.getByRole('alert')).toContainText('上传暂不可用');
  await expect(modal.getByLabel('资料地址')).toHaveValue('intranet/manual');
  await expect(modal.getByLabel('资料名称')).toHaveValue('导入资料.txt');
  await modal.getByLabel('导入资料文件').setInputFiles(file);
  await expect(modal.getByLabel('资料地址')).toHaveValue('.flowweave/cases/import-case/materials/imported-file');
  expect(writes).toEqual([]);
  expect(item.description).toBe('原有说明');
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 768 });
    const address = await modal.getByLabel('资料地址').boundingBox();
    const upload = await modal.getByRole('button', { name: '导入', exact: true }).boundingBox();
    expect(Math.round(address!.y)).toBe(Math.round(upload!.y));
    expect(upload!.width).toBeGreaterThan(50);
    expect(await modal.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: `../../.tmp/ui-697/material-dialog-${width}.png` });
  }
  await modal.getByRole('button', { name: '取消', exact: true }).click();
  await expect(modal).toHaveCount(0);
  expect(writes[0]).toMatchObject({ description: '原有说明', materials: [{ name: '已保存资料' }] });
  await expect(page.getByRole('textbox', { name: '说明', exact: true })).toHaveValue('原有说明');
  await expect(page.getByRole('button', { name: '添加资料', exact: true })).toBeFocused();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.case-inspector').getByRole('status')).toHaveText('已保存');
  expect(item.description).toBe('原有说明');
  expect(item.materials).toHaveLength(1);
});

test('case material accepts arbitrary reference text without making script references executable', async ({ page }) => {
  await openWorkspace(page);
  let saved: Record<string, unknown> | undefined;
  const item = { id: 'references-case', name: '任意资料', description: '', objective: '', row_version: 1, state: 'DRAFT', materials: [], stages: [], tasks: [], control_plans: [], decision_proposals: [], created_at: now, updated_at: now };
  await page.route('**/api/v1/cases**', async route => {
    if (route.request().method() === 'GET') return json(route, [item]);
    saved = route.request().postDataJSON();
    return json(route, { ...item, ...saved, row_version: 2 });
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await openCasePanel(page, '场景信息');
  const values = ['feishu://docs/item', 'ftp://example.test/input', 'intranet/wiki/page', 'javascript:window.__caseScriptExecuted=true'];
  for (const [index, value] of values.entries()) {
    await page.getByRole('button', { name: '添加资料', exact: true }).click();
    const modal = page.getByRole('dialog', { name: '添加资料', exact: true });
    await modal.getByLabel('资料名称').fill(`资料 ${index + 1}`);
    await modal.getByLabel('资料地址').fill(value);
    await modal.getByRole('button', { name: '添加', exact: true }).click();
    await expect(modal).toHaveCount(0);
  }
  expect(saved).toBeUndefined();
  const inert = page.locator('.scenario-materials a').filter({ hasText: '资料 4' });
  await expect(inert).not.toHaveAttribute('href');
  await inert.click();
  expect(await page.evaluate(() => '__caseScriptExecuted' in window)).toBe(false);
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(() => saved?.materials).toEqual(values.map((value, index) => ({ kind: 'LINK', name: `资料 ${index + 1}`, value })));
});

test('new case material upload retry reuses the created scene and saves its relative file reference', async ({ page }) => {
  await openWorkspace(page);
  let creates = 0, uploads = 0;
  let item: Record<string, unknown> | undefined;
  await page.route('**/api/v1/cases**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'GET') return json(route, item ? [item] : []);
    if (url.pathname.endsWith('/materials')) {
      uploads += 1;
      if (uploads === 1) return json(route, { error: { code: 'UPLOAD_FAILED', message: '请重试上传' } }, 503);
      expect(url.searchParams.get('expected_row_version')).toBe('1');
      item = { ...item, row_version: 2, materials: [{ kind: 'FILE', name: 'input.txt', value: '.flowweave/cases/new-import/materials/file-1' }] };
      return json(route, item, 201);
    }
    const body = request.postDataJSON();
    if (request.method() === 'POST') {
      creates += 1;
      item = { ...body, id: 'new-import', state: 'DRAFT', row_version: 1, stages: [], tasks: [], control_plans: [], decision_proposals: [], created_at: now, updated_at: now };
    } else {
      expect(body.expected_row_version).toBe(2);
      item = { ...item, ...body, row_version: 3 };
    }
    return json(route, item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '新建场景', exact: true }).click();
  await page.getByLabel('名称', { exact: true }).fill('新建资料场景');
  await expect(page.getByRole('button', { name: '添加资料', exact: true })).toHaveCount(0);
  expect(creates).toBe(0);
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.scenario-path')).toBeVisible();
  expect(creates).toBe(1);
  await openCasePanel(page, '场景信息');
  await page.getByRole('button', { name: '添加资料', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '添加资料', exact: true });
  const file = { name: 'input.txt', mimeType: 'text/plain', buffer: Buffer.from('inputs') };
  await modal.getByLabel('导入资料文件').setInputFiles(file);
  await expect(modal.getByRole('alert')).toContainText('请重试上传');
  await modal.getByLabel('导入资料文件').setInputFiles(file);
  await expect(modal.getByLabel('资料地址')).toHaveValue('.flowweave/cases/new-import/materials/file-1');
  await expect(modal.getByLabel('资料名称')).toHaveValue('input.txt');
  await modal.getByRole('button', { name: '添加', exact: true }).click();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.scenario-path')).toBeVisible();
  expect(creates).toBe(1);
  expect(uploads).toBe(2);
  expect(item?.materials).toEqual([{ kind: 'FILE', name: 'input.txt', value: '.flowweave/cases/new-import/materials/file-1' }]);
});


test('workspace sidebar toggle and inline capability trigger preserve the active draft', async ({ page }) => {
  await openWorkspace(page);
  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  const input = page.getByLabel('发送 Agent 消息', { exact: true });
  await input.fill('请使用@文件 后续内容');
  await expect(page.getByRole('listbox', { name: '选择引用类型' })).toHaveCount(0);
  await input.fill('请使用 @文件 后续内容');
  await input.press('Home');
  for (let index = 0; index < 7; index += 1) await input.press('ArrowRight');
  await expect(page.getByRole('listbox', { name: '选择引用类型' })).toBeVisible();
  await input.press('Escape');
  await expect(input).toHaveValue('请使用  后续内容');
  const home = page.getByRole('button', { name: '主页', exact: true });
  await home.click();
  await expect(home).toHaveAttribute('aria-pressed', 'true');
  await expect(input).toHaveValue('请使用  后续内容');
  await home.click();
  await expect(home).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('[data-workspace-group-id="root"]').getByRole('button', { name: '日报会话', exact: true })).toBeVisible();
  await expect(input).toHaveValue('请使用  后续内容');
});

test('workspace action menu defaults to all credentials and persists session configuration', async ({ page }) => {
  await openWorkspace(page);
  const root = page.locator('[data-workspace-group-id="root"]');
  await root.locator('header').hover();
  await root.getByRole('button', { name: '工作区操作 根工作区', exact: true }).click({ force: true });
  const menu = page.getByRole('menu', { name: '工作区操作菜单：根工作区', exact: true });
  await expect(menu.getByRole('menuitem', { name: '会话配置', exact: true })).toBeVisible();
  await menu.getByRole('menuitem', { name: '会话配置', exact: true }).click();

  const dialog = page.getByRole('dialog', { name: '会话配置', exact: true });
  await dialog.getByRole('button', { name: /日报技能/, exact: false }).click();
  await dialog.getByRole('button', { name: '认证', exact: true }).click();
  await expect(dialog.getByRole('button', { name: /测试认证/, exact: false })).toHaveAttribute('aria-pressed', 'true');
  await dialog.getByRole('button', { name: '保存认证配置', exact: true }).click();
  await expect(dialog).toHaveCount(0);

  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  await root.locator('header').hover();
  await root.getByRole('button', { name: '在根工作区中新建会话', exact: true }).click({ force: true });
  await page.getByRole('button', { name: '新会话配置', exact: true }).click();
  const inherited = page.getByRole('dialog', { name: '会话配置', exact: true });
  await expect(inherited.getByRole('button', { name: /日报技能/, exact: false })).toHaveClass(/selected/);
  await inherited.getByRole('button', { name: '认证', exact: true }).click();
  await expect(inherited.getByRole('button', { name: /测试认证/, exact: false })).toHaveAttribute('aria-pressed', 'true');
  await inherited.getByRole('button', { name: '取消全选认证', exact: true }).click();
  await expect(inherited.getByText('已选择 0 项认证', { exact: true })).toBeVisible();
});

test('named workspaces keep session configuration separate from root defaults', async ({ page }) => {
  await openWorkspace(page, [], false, '日报会话', {
    defaultCapabilityIds: ['capability-1'],
    defaultCredentialIds: ['credential-1'],
  });
  const directory = page.locator('[data-workspace-group-id="existing-directory"]');
  await directory.locator('header').hover();
  await directory.getByRole('button', { name: '工作区操作 已有项目目录', exact: true }).click({ force: true });
  await page.getByRole('menu', { name: '工作区操作菜单：已有项目目录', exact: true }).getByRole('menuitem', { name: '会话配置', exact: true }).click();

  const configuration = page.getByRole('dialog', { name: '会话配置', exact: true });
  await expect(configuration.getByRole('button', { name: /日报技能/, exact: false })).not.toHaveClass(/selected/);
  await configuration.getByRole('button', { name: /周报技能/, exact: false }).click();
  await configuration.getByRole('button', { name: '认证', exact: true }).click();
  await configuration.getByRole('button', { name: /测试认证/, exact: false }).click();
  await configuration.getByRole('button', { name: '保存认证配置', exact: true }).click();

  await directory.locator('header').hover();
  await directory.getByRole('button', { name: '在已有项目目录中新建会话', exact: true }).click({ force: true });
  await page.getByRole('button', { name: '新会话配置', exact: true }).click();
  const inherited = page.getByRole('dialog', { name: '会话配置', exact: true });
  await expect(inherited.getByRole('button', { name: /日报技能/, exact: false })).not.toHaveClass(/selected/);
  await expect(inherited.getByRole('button', { name: /周报技能/, exact: false })).toHaveClass(/selected/);
  await inherited.getByRole('button', { name: '认证', exact: true }).click();
  await expect(inherited.getByRole('button', { name: /测试认证/, exact: false })).toHaveAttribute('aria-pressed', 'true');
  await inherited.getByRole('button', { name: /测试认证/, exact: false }).click();
  await inherited.getByRole('button', { name: '保存认证配置', exact: true }).click();

  await page.getByRole('button', { name: '新会话配置', exact: true }).click();
  const customized = page.getByRole('dialog', { name: '会话配置', exact: true });
  await expect(customized.getByRole('button', { name: /周报技能/, exact: false })).toHaveClass(/selected/);
  await customized.getByRole('button', { name: '认证', exact: true }).click();
  await expect(customized.getByRole('button', { name: /测试认证/, exact: false })).toHaveAttribute('aria-pressed', 'false');
  await customized.getByRole('button', { name: '关闭会话配置', exact: true }).click();

  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  await directory.locator('header').hover();
  await directory.getByRole('button', { name: '在已有项目目录中新建会话', exact: true }).click({ force: true });
  await page.getByRole('button', { name: '新会话配置', exact: true }).click();
  const fresh = page.getByRole('dialog', { name: '会话配置', exact: true });
  await fresh.getByRole('button', { name: '认证', exact: true }).click();
  await expect(fresh.getByRole('button', { name: /测试认证/, exact: false })).toHaveAttribute('aria-pressed', 'true');
});


test('temporary services stop through the scoped workspace API and refresh the card', async ({ page }) => {
  await openWorkspace(page);
  let stopped = false;
  await page.route(/\/api\/v1\/agent-workspaces\/schedule-workspace\/workspace(?:\?.*)?$/, route => json(route, {
    root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
    files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } },
    temporary_services: stopped ? [] : [{ id: 'preview-service', source: 'DISCOVERED', state: 'RUNNING', pid: 91, command: 'vite', ports: [5173], url: 'http://127.0.0.1:5173/', last_seen_at: now }],
  }));
  await page.route('**/api/v1/agent-workspaces/schedule-workspace/temporary-services/preview-service/stop', route => {
    expect(route.request().method()).toBe('POST');
    stopped = true;
    return json(route, { stopped: true }, 202);
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  const services = page.locator('.agent-workspace-temporary-services');
  await expect(services).toContainText('http://127.0.0.1:5173/');
  await services.getByRole('button', { name: '停止', exact: true }).click();
  await expect(services).toHaveCount(0);
  expect(stopped).toBe(true);
});


test('workspace source summary shows two items and keeps all sources discoverable', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'source-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看 https://example.test/source', timestamp: now, attachments: [
      { filename: 'first.txt', mime_type: 'text/plain', byte_size: 8, path: '/runtime/workspace/project/uploads/first.txt' },
      { filename: 'second.txt', mime_type: 'text/plain', byte_size: 8, path: '/runtime/workspace/project/uploads/second.txt' },
    ] } },
    { id: 'source-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'source-user', content: '已读取资料。', final_reply: true, timestamp: now } },
  ]);
  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  const summary = page.locator('.agent-workspace-source-list');
  await expect(summary.locator('button b')).toHaveText(['first.txt', 'second.txt']);
  await expect(page.getByRole('button', { name: '查看全部', exact: true })).toBeVisible();
  const viewAll = page.getByRole('button', { name: '查看全部', exact: true });
  await expect(viewAll).toHaveCSS('border-top-width', '0px');
  await expect(viewAll.locator('span')).toHaveCSS('font-weight', '400');
  await page.getByLabel('搜索当前会话').focus();
  await page.locator('.agent-workspace-summary').screenshot({ path: '../../.tmp/sources-refined.png' });
  await viewAll.click();
  const sources = page.getByRole('region', { name: '全部会话来源' });
  await expect(sources).toContainText('first.txt');
  await expect(sources).toContainText('second.txt');
  await expect(sources).toContainText('example.test/source');
});


test('conversation search highlights all occurrences and navigates one active match with Enter', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'search-text-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请核对成功样本与状态。', timestamp: now } },
    { id: 'search-text-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'search-text-user', content: '已补充**成功**样本的详细信息。\n\n下载：[成功样本.xlsx](https://example.test/成功/result.xlsx)\n\n状态为 `成功`，Token 与 `token` 均已核对。\n\n字面值：`[x].*`。\n\n跨**格式**匹配。', final_reply: true, timestamp: now } },
  ], true);
  await page.goto('/agent/conversations/scheduled-conversation');
  const search = page.locator('.agent-workspace-summary').getByRole('search');
  const input = search.getByLabel('搜索当前会话');
  const highlights = page.locator('.agent-workbench-main .conversation-search-highlights i');
  const active = highlights.and(page.locator('.is-active'));
  const user = page.locator('[data-conversation-event-id="search-text-user"]');
  const reply = page.locator('[data-conversation-event-id="search-text-reply"]');
  const submit = async (query: string) => {
    await input.fill(query);
    await input.press('Enter');
  };
  const expectActive = async (index: number, eventId: string, matchIndex: number) => {
    await expect(search.locator('output')).toHaveText(`${index} 个结果，共 4 个结果`);
    await expect(highlights).toHaveCount(4);
    await expect(active).toHaveCount(1);
    await expect(active).toHaveAttribute('data-search-event-id', eventId);
    await expect(active).toHaveAttribute('data-search-match-index', String(matchIndex));
  };
  await submit('成功');
  await expectActive(1, 'search-text-user', 0);
  await expect(input).toBeFocused();
  await input.press('Enter');
  await expectActive(2, 'search-text-reply', 0);
  await input.press('Enter');
  await expectActive(3, 'search-text-reply', 1);
  await input.press('Shift+Enter');
  await expectActive(2, 'search-text-reply', 0);
  await search.getByRole('button', { name: '上一个搜索结果' }).click();
  await expectActive(1, 'search-text-user', 0);
  await input.press('Shift+Enter');
  await expectActive(4, 'search-text-reply', 2);
  await search.getByRole('button', { name: '下一个搜索结果' }).click();
  await expectActive(1, 'search-text-user', 0);
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  await expectActive(1, 'search-text-user', 0);
  await settleWorkspaceLayout(page);
  const colors = await highlights.evaluateAll(elements => elements.map(element => ({ active: element.classList.contains('is-active'), color: getComputedStyle(element).backgroundColor })));
  expect(colors.find(item => item.active)?.color).not.toBe(colors.find(item => !item.active)?.color);
  // All overlays coincide with matched text, including bold, links and code.
  const expected = await reply.locator('strong, a, code').evaluateAll(elements => elements.slice(0, 3).map(element => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let text = walker.nextNode() as Text | null;
    while (text && !text.data.includes('成功')) text = walker.nextNode() as Text | null;
    if (!text) throw new Error('Expected a rendered search match');
    const offset = text.data.indexOf('成功');
    const range = document.createRange();
    range.setStart(text, offset);
    range.setEnd(text, offset + 2);
    const rect = range.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));
  const replyRects = await highlights.and(page.locator('[data-search-event-id="search-text-reply"]')).evaluateAll(elements => elements.map(element => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));
  for (let index = 0; index < expected.length; index += 1) {
    for (const key of ['x', 'y', 'width', 'height'] as const) expect(Math.abs(replyRects[index][key] - expected[index][key]), JSON.stringify({ index, key, actual: replyRects[index], expected: expected[index] })).toBeLessThanOrEqual(1);
    expect(replyRects[index].width).toBeLessThan(100);
    expect(replyRects[index].height).toBeLessThan(35);
  }
  expect((await user.boundingBox())!.width).toBeGreaterThan((await active.boundingBox())!.width);
  await page.screenshot({ path: '../../.tmp/conversation-search-occurrences.png' });
  await page.setViewportSize({ width: 1024, height: 800 });
  await settleWorkspaceLayout(page);
  await expectActive(1, 'search-text-user', 0);
  await submit('TOKEN');
  await expect(search.locator('output')).toHaveText('1 个结果，共 2 个结果');
  await expect(highlights).toHaveCount(2);
  await input.press('Enter');
  await expect(search.locator('output')).toHaveText('2 个结果，共 2 个结果');
  await expect(active).toHaveAttribute('data-search-match-index', '1');
  await submit('[x].*');
  await expect(highlights).toHaveCount(1);
  await expect(active).toHaveCount(1);
  await submit('跨格式匹配');
  await expect(search.locator('output')).toHaveText('1 个结果，共 1 个结果');
  await expect(active).toHaveCount(1);
  await submit('example.test');
  await expect(search.locator('output')).toHaveText('0 个结果');
  await expect(highlights).toHaveCount(0);
  await submit('成功');
  await expectActive(1, 'search-text-user', 0);
  await search.getByRole('button', { name: '清除搜索' }).click();
  await expect(highlights).toHaveCount(0);
  await submit('成功');
  await expectActive(1, 'search-text-user', 0);
  await input.press('Escape');
  await expect(highlights).toHaveCount(0);
  await submit('成功');
  await page.getByRole('button', { name: '周报会话', exact: true }).click();
  await expect(page).toHaveURL(/\/scheduled-conversation-2$/);
  await expect(input).toHaveValue('');
  await expect(highlights).toHaveCount(0);
  await page.getByRole('button', { name: '日报会话', exact: true }).click();
  await expect(input).toHaveValue('');
  await expect(highlights).toHaveCount(0);
});


test('conversation search scrolls to each occurrence within a long reply', async ({ page }) => {
  const content = ['第一处定位词。', ...Array.from({ length: 45 }, (_, i) => `第${i + 1}段填充内容。`), '第二处定位词。', ...Array.from({ length: 45 }, (_, i) => `第${i + 46}段填充内容。`), '第三处定位词。'].join('\n\n');
  await openWorkspace(page, [
    { id: 'search-long-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请查看长回复', timestamp: now } },
    { id: 'search-long-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'search-long-user', content, final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const search = page.locator('.agent-workspace-summary').getByRole('search');
  const input = search.getByLabel('搜索当前会话');
  const active = page.locator('.agent-workbench-main .conversation-search-highlights i.is-active');
  const surface = page.locator('.agent-workbench-main .conversation-surface');
  await input.fill('定位词');
  await input.press('Enter');
  for (let index = 0; index < 3; index += 1) {
    await expect(search.locator('output')).toHaveText(`${index + 1} 个结果，共 3 个结果`);
    await expect(active).toHaveCount(1);
    await expect(active).toHaveAttribute('data-search-match-index', String(index));
    await expect.poll(async () => {
      const hit = (await active.boundingBox())!;
      const bounds = (await surface.boundingBox())!;
      return hit.y >= bounds.y && hit.y + hit.height <= bounds.y + bounds.height;
    }).toBe(true);
    if (index < 2) await input.press('Enter');
  }
  await input.press('Shift+Enter');
  await expect(active).toHaveAttribute('data-search-match-index', '1');
  await expect(input).toBeFocused();
});


test('conversation search shares the floating card visibility width and separate surface', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'floating-search-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '搜索布局检查', timestamp: now } },
    { id: 'floating-search-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'floating-search-user', content: '搜索布局已检查', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  const group = page.locator('.agent-workspace-summary');
  const search = group.getByRole('search');
  const input = search.getByLabel('搜索当前会话');
  const unfocusedShadow = await search.evaluate(element => getComputedStyle(element).boxShadow);
  await input.focus();
  await expect(search).toHaveCSS('box-shadow', unfocusedShadow);
  await expect(group).toHaveCSS('filter', 'none');
  const overview = group.locator('.agent-workspace-summary-card');
  const expectGeometry = async () => {
    await settleWorkspaceLayout(page);
    await expect(search).toBeVisible();
    await expect(overview).toBeVisible();
    const a = (await search.boundingBox())!;
    const b = (await overview.boundingBox())!;
    expect(Math.abs(a.x - b.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(a.width - b.width)).toBeLessThanOrEqual(1);
    expect(b.y - (a.y + a.height)).toBeGreaterThanOrEqual(8);
    expect(b.y - (a.y + a.height)).toBeLessThanOrEqual(12);
    expect(await search.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await expect(page.locator('.agent-composer-dock').getByRole('search')).toHaveCount(0);
  };
  for (const width of [1920, 1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expectGeometry();
    await input.fill('搜索');
    await input.press('Enter');
    await expect(search.getByText('1 个结果，共 2 个结果')).toBeVisible();
    await expectGeometry();
    await search.getByRole('button', { name: '下一个搜索结果' }).click();
    await expect(search.getByText('2 个结果，共 2 个结果')).toBeVisible();
    await page.getByRole('button', { name: '隐藏悬浮信息卡', exact: true }).first().click();
    await expect(search).toBeHidden();
    await expect(overview).toBeHidden();
    await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).first().click();
    await expectGeometry();
    await search.getByRole('button', { name: '清除搜索' }).click();
  }
  await page.setViewportSize({ width: 1920, height: 1000 });
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await expect(search).toBeHidden();
  await page.getByRole('button', { name: '显示悬浮信息卡', exact: true }).first().click();
  await expectGeometry();
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const edge = (await resizer.boundingBox())!;
  await page.mouse.move(edge.x + edge.width / 2, edge.y + edge.height / 2);
  await page.mouse.down();
  await page.mouse.move(edge.x - 200, edge.y + edge.height / 2, { steps: 10 });
  await page.mouse.up();
  await expectGeometry();
  await page.getByRole('button', { name: '全屏查看工作区工具', exact: true }).click();
  await expect(search).toBeHidden();
  await page.getByRole('button', { name: '日报会话', exact: true }).last().click();
  await expectGeometry();
  await page.getByRole('button', { name: '退出全屏', exact: true }).first().click();
  await page.getByRole('button', { name: '关闭工作区工具', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expectGeometry();
  await group.screenshot({ path: '../../.tmp/search-card-corrected.png' });
});


for (const sourceCount of [0, 1, 2]) {
  test(`workspace source summary keeps ${sourceCount} sources inline without view all`, async ({ page }) => {
    await openWorkspace(page, [
      { id: 'source-count-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '查看资料', timestamp: now, attachments: Array.from({ length: sourceCount }, (_, index) => ({ filename: `source-${index + 1}.txt`, mime_type: 'text/plain', byte_size: 8, path: `/runtime/workspace/project/uploads/source-${index + 1}.txt` })) } },
      { id: 'source-count-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'source-count-user', content: '已读取资料。', final_reply: true, timestamp: now } },
    ]);
    await page.goto('/agent/conversations/scheduled-conversation');
    await expect(page.locator('.agent-workspace-summary')).toBeVisible();
    await expect(page.locator('.agent-workspace-source-list > button')).toHaveCount(sourceCount);
    await expect(page.getByRole('button', { name: '查看全部', exact: true })).toHaveCount(0);
  });
}

test('case path fills the workspace with compact stages and on-demand context and control records', async ({ page }) => {
  await openWorkspace(page);
  let item = {
    id: 'path-case', name: '客户上线场景', description: '客户背景说明', objective: '完成迁移、验证并交付', work_directory_id: 'existing-directory', control_work_directory_id: 'case-directory',
    state: 'ACTIVE', row_version: 1, materials: [], flow_run_ids: [], created_at: now, updated_at: now,
    session_config: { model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', capability_version_ids: [], credential_ids: [] },
    tasks: [], decision_proposals: [], control_plans: [{ id: 'plan', case_id: 'path-case', stage_id: 'stage-1', action: 'START_EXECUTION', state: 'FAILED', reason: '等待环境恢复后重试', error_code: 'ENVIRONMENT_NOT_READY', created_at: now }],
    stages: Array.from({ length: 12 }, (_, index) => ({ id: `stage-${index}`, case_id: 'path-case', stage_key: `stage-${index}`, name: ['资料准备', '迁移执行', '质量验证', '结果交付'][index % 4] + ` ${index + 1}`, objective: '', flow_run_id: `template-${index}`, position: index, execution_mode: index % 2 ? 'CONTINUOUS' : 'STEPWISE', state: index === 0 ? 'COMPLETED' : index === 1 ? 'ACTIVE' : 'PENDING', current_execution_flow_run_id: index < 2 ? `execution-${index}` : null, execution: index < 2 ? { state: index === 0 ? 'COMPLETED' : 'CANCELLED', progress: { accepted: index === 0 ? 4 : 2, terminal: 4, active: 0 }, active_node: null } : null })),
  };
  await page.route('**/api/v1/cases**', route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'PUT') {
      const payload = route.request().postDataJSON();
      expect(payload.name).toBe(item.name); expect(payload.work_directory_id).toBe('existing-directory');
      item = { ...item, ...payload, row_version: item.row_version + 1 };
      return json(route, item);
    }
    return json(route, path.endsWith('/cases') ? [item] : item);
  });
  await page.goto('/?view=cases');
  await expect(page.locator('.case-config-panel')).toHaveCount(0);
  await expect(page.locator('.scenario-path ol li')).toHaveCount(12);
  const current = page.getByRole('article', { name: '路径卡片 迁移执行 2', exact: true });
  await expect(current).toContainText('已停止'); await expect(current).not.toContainText('运行中');
  await expect(current.getByRole('button', { name: '查看执行记录' })).toBeVisible();
  const flowInfo = page.getByRole('complementary', { name: '流程信息', exact: true });
  await expect(flowInfo).toContainText('迁移执行 2');
  await expect(flowInfo).toContainText('2 / 12');
  const recordEntry = flowInfo.getByRole('button', { name: /查看流程记录/ });
  await expect(recordEntry).toBeVisible();
  await page.getByRole('article', { name: '路径卡片 质量验证 3', exact: true }).click();
  await expect(flowInfo).toContainText('质量验证 3');
  await expect(flowInfo).toContainText('启动后可从这里进入对应的流程记录');
  await current.click();
  await recordEntry.click();
  await expect(page).toHaveURL(/\/flow-runs\/execution-1$/);
  await page.goto('/?view=cases');
  await expect(flowInfo).toBeVisible();
  await expect(page.getByRole('button', { name: '保存基础信息' })).toHaveCount(0);
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 768 });
    const bounds = await page.locator('.case-path-panel').evaluate(element => ({ width: element.clientWidth, scroll: element.scrollHeight, height: element.clientHeight, main: element.closest('.scenario-workspace')!.clientWidth }));
    expect(bounds.width).toBeGreaterThan(bounds.main * 0.9);
    const path = await page.locator('.case-connected-path').boundingBox();
    const info = await flowInfo.boundingBox();
    expect(path!.width).toBeGreaterThan(bounds.width - info!.width - 100);
    expect(info!.width).toBeGreaterThan(230);
    expect(bounds.scroll).toBeGreaterThan(bounds.height);
    expect((await page.locator('.case-workspace-toolbar').boundingBox())!.height).toBeLessThanOrEqual(56);
    await page.locator('.case-path-panel').hover(); await page.mouse.wheel(0, 600);
    await expect.poll(() => page.locator('.case-path-panel').evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    expect(await page.evaluate(() => ({ width: document.documentElement.scrollWidth, scroll: document.documentElement.scrollHeight, viewport: innerHeight }))).toEqual({ width, scroll: 768, viewport: 768 });
    await page.locator('.case-path-panel').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: `../../.tmp/ops35/case-path-${width}.png` });
  }
  await expect(page.getByText('Case 路径', { exact: true })).toHaveCount(0);
  await expect(page.getByText('拖拽排序', { exact: true })).toHaveCount(0);
  expect((await current.boundingBox())!.height).toBeLessThan(145);
  await openCasePanel(page, 'Agent 配置');
  const agent = page.getByRole('dialog', { name: 'Agent 配置', exact: true });
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 768 });
    expect((await agent.boundingBox())!.height).toBeLessThan(360);
    await agent.getByRole('combobox', { name: '思考程度', exact: true }).click();
    await expect(agent.getByRole('option', { name: 'high', exact: true })).toBeVisible();
    await page.screenshot({ path: `../../.tmp/ops35/agent-configuration-${width}.png` });
    await page.keyboard.press('Escape');
  }
  await openCasePanel(page, '场景信息');
  const context = page.getByRole('dialog', { name: '场景信息', exact: true });
  await expect(context.getByRole('textbox', { name: '名称', exact: true })).toHaveAttribute('readonly');
  await expect(context.getByRole('combobox', { name: '工作区', exact: true })).toBeDisabled();
  await expect(context.getByRole('textbox', { name: '目标', exact: true })).toHaveAttribute('readonly');
  await expect(context.getByRole('textbox', { name: '说明', exact: true })).toHaveAttribute('readonly');
  expect((await context.boundingBox())!.height).toBeLessThan(600);
  await page.screenshot({ path: '../../.tmp/ops35/scene-information.png' });
  await context.getByRole('button', { name: '保存', exact: true }).click();
  await expect(context.getByRole('status')).toHaveText('已保存');
  expect(item.objective).toBe('完成迁移、验证并交付');
  await page.keyboard.press('Escape'); await expect(context).toHaveCount(0);
  await expect(page.locator('.case-workspace-toolbar').getByRole('button', { name: /操作记录/ })).toHaveCount(0);
  await openCasePanel(page, '场景信息');
  await page.getByRole('button', { name: '查看操作记录', exact: true }).click();
  const activity = page.getByRole('dialog', { name: '操作记录', exact: true });
  await expect(activity).toContainText('等待环境恢复后重试');
  await expect(activity).toContainText('ENVIRONMENT_NOT_READY');
  await page.mouse.click(8, 150); await expect(activity).toHaveCount(0);
  await page.getByRole('textbox', { name: '搜索场景' }).fill('不存在的场景');
  await expect(page.locator('.scenario-case-head')).toHaveCount(0);
});

test('case Agent start checks configuration and retries using the same submission identity', async ({ page }) => {
  await openWorkspace(page);
  let item: Record<string, unknown> = { id: 'start-case', name: '上线场景', objective: '交付', row_version: 1, state: 'DRAFT', stages: [], tasks: [], control_plans: [], decision_proposals: [], materials: [], created_at: now, updated_at: now };
  const keys: string[] = [];
  await page.route('**/api/v1/cases**', route => {
    const path = new URL(route.request().url()).pathname;
    const request = route.request();
    if (path.endsWith('/control-conversation') && request.method() === 'POST') {
      keys.push(request.headers()['idempotency-key']);
      if (keys.length === 1) return json(route, { error: { code: 'TEMPORARY_FAILURE', message: '创建暂不可用，请重试' } }, 503);
      item = { ...item, control_conversation_binding_id: 'control-binding', control_work_directory_id: 'case-directory', row_version: 3 };
      return json(route, item);
    }
    if (path.endsWith('/session-config') && request.method() === 'PUT') {
      const { expected_row_version: version, ...config } = request.postDataJSON();
      expect(version).toBe(1); item = { ...item, session_config: config, row_version: 2 };
      return json(route, item);
    }
    return json(route, path.endsWith('/cases') ? [item] : item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await expect(page.getByRole('button', { name: '启动 Agent', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '启动 Agent', exact: true })).toHaveAttribute('title', '请先添加完整的流程路径、保存 Agent 配置');
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(keys).toEqual([]);
  item = { ...item, stages: [{ id: 'stage', name: '交付流程', flow_run_id: 'template', state: 'ACTIVE', position: 0, execution_mode: 'STEPWISE' }] };
  await page.reload();
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await expect(page.getByRole('button', { name: '启动 Agent', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '启动 Agent', exact: true })).toHaveAttribute('title', '请先保存 Agent 配置');
  expect(keys).toEqual([]);
  await openCasePanel(page, 'Agent 配置');
  const config = page.getByRole('dialog', { name: 'Agent 配置', exact: true });
  await expect(config).toBeVisible();
  await config.getByRole('combobox', { name: '模型供应商' }).click();
  await page.getByRole('option', { name: '测试供应商', exact: true }).click();
  await config.getByRole('combobox', { name: '模型名称' }).click();
  await page.getByRole('option', { name: 'model-1', exact: true }).click();
  await config.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(config.getByRole('status')).toHaveText('已保存');
  await page.getByRole('button', { name: '关闭场景面板' }).click();
  await expect(page.getByRole('button', { name: '启动 Agent', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '启动 Agent', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('创建暂不可用');
  await page.getByRole('button', { name: '启动 Agent', exact: true }).click();
  await expect(page).toHaveURL(/\/cases\/start-case\/conversations\/control-binding$/);
  expect(keys).toHaveLength(2); expect(keys[0]).toBeTruthy(); expect(keys[0]).toBe(keys[1]);
});

test('case coordination rounds wait for background delivery retry and retain history', async ({ page }) => {
  await openWorkspace(page);
  await page.clock.install();
  let item: Record<string, unknown> = {
    id: 'round-case', name: '交付协调', objective: '完整交付', state: 'ACTIVE', row_version: 1,
    stages: [{ id: 'stage', name: '交付流程', flow_run_id: 'template', state: 'ACTIVE', position: 0, execution_mode: 'CONTINUOUS' }],
    session_config: { model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', capability_version_ids: [], credential_ids: [] },
    tasks: [], control_plans: [], decision_proposals: [], materials: [], control_rounds: [], created_at: now, updated_at: now,
  };
  const starts: string[] = [];
  let retries = 0;
  const round = (id: string, state: string, current = true) => ({ id, state, binding_id: state === 'STARTED' ? `${id}-binding` : null, is_current: current, error_code: state === 'FAILED' ? 'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS' : null, created_at: now, started_at: state === 'STARTED' ? now : null });
  await page.route('**/api/v1/cases**', route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && pathname.endsWith('/control-conversation')) {
      starts.push(request.headers()['idempotency-key']);
      const id = `round-${starts.length}`;
      item = { ...item, current_control_round_id: id, control_conversation_binding_id: null, control_work_directory_id: 'case-directory', control_rounds: [round(id, 'PENDING'), ...(starts.length > 1 ? [round('round-1', 'STARTED', false)] : [])], row_version: Number(item.row_version) + 1 };
      return json(route, item, 201);
    }
    if (request.method() === 'POST' && pathname.endsWith('/retry')) {
      retries += 1;
      item = { ...item, control_rounds: [round('round-1', 'PENDING')], row_version: Number(item.row_version) + 1 };
      return json(route, item);
    }
    return json(route, pathname.endsWith('/cases') ? [item] : item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '启动 Agent', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建会话中…', exact: true })).toBeDisabled();
  await expect(page).not.toHaveURL(/\/conversations\//);
  item = { ...item, control_rounds: [round('round-1', 'FAILED')], row_version: 3 };
  await page.clock.fastForward(3100);
  await expect(page.getByRole('alert')).toContainText('AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS');
  await page.getByRole('button', { name: '重试启动', exact: true }).click();
  expect(retries).toBe(1); expect(starts).toHaveLength(1);
  item = { ...item, control_rounds: [round('round-1', 'STARTED')], control_conversation_binding_id: 'round-1-binding', row_version: 5 };
  await page.clock.fastForward(3100);
  await expect(page).toHaveURL(/\/cases\/round-case\/conversations\/round-1-binding$/);
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '协调会话', exact: true }).click();
  await page.getByRole('button', { name: '新建协调轮次', exact: true }).click();
  expect(starts).toHaveLength(2); expect(starts[0]).not.toBe(starts[1]);
  await page.getByRole('button', { name: '协调会话', exact: true }).click();
  const history = page.getByRole('dialog', { name: '协调会话', exact: true });
  await expect(history).toContainText('历史轮次');
  await expect(history).toContainText('当前轮次');
  await history.getByRole('button', { name: '打开会话', exact: true }).click();
  await expect(page).toHaveURL(/\/cases\/round-case\/conversations\/round-1-binding$/);
  await expect(page.getByText('该会话不属于当前事项')).toHaveCount(0);
});


test('case workspace presents review, feedback and governed takeover actions', async ({ page }) => {
  await openWorkspace(page);
  let item = {
    id: 'takeover-case', name: '交付复核', objective: '确认结果并交付', state: 'ACTIVE', row_version: 8,
    control_conversation_binding_id: 'case-agent-binding', materials: [], decision_proposals: [], tasks: [],
    feedback_deliveries: [{ id: 'delivery-1', round_id: 'round', binding_id: 'case-agent-binding', source_kind: 'EXECUTION', execution_event_id: 'event-1', state: 'FAILED', batch_id: null, message_event_id: null, error_code: 'CASE_FEEDBACK_RETRIES_EXHAUSTED' }],
    control_plans: [{ id: 'failed-plan', case_id: 'takeover-case', stage_id: 'stage', source_binding_id: 'case-agent-binding', execution_flow_run_id: 'execution', action: 'ADVANCE_CASE_STAGE', state: 'FAILED', idempotency_key: 'failed-plan', reason: '结果符合目标，进入下一阶段', input_snapshot: {}, result: {}, error_code: 'CASE_FLOW_VERSION_CONFLICT', created_at: now, validated_at: now, executed_at: now }],
    stages: [{ id: 'stage', case_id: 'takeover-case', stage_key: 'review', name: '质量复核', objective: '核对正式交付物', position: 0, flow_run_id: 'template', current_execution_flow_run_id: 'execution', execution_mode: 'CONTINUOUS', state: 'REVIEW_PENDING', execution: { flow_run_id: 'execution', state: 'COMPLETED', progress: { accepted: 2, terminal: 2, active: 0 }, active_node: null, ready_node_keys: [], automatic_block: null, finished_at: now }, created_at: now, updated_at: now }],
    control_rounds: [{ id: 'round', binding_id: 'case-agent-binding', state: 'STARTED', is_current: true, error_code: null, created_at: now, started_at: now }],
    session_config: { model_provider_id: 'provider-1', model_name: 'model-1', reasoning_effort: 'high', capability_version_ids: [], credential_ids: [] }, created_at: now, updated_at: now,
  };
  const coordinationStates: string[] = [];
  let retried = 0;
  await page.route('**/api/v1/cases**', route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith('/coordination-state') && request.method() === 'PUT') {
      const payload = request.postDataJSON(); coordinationStates.push(payload.state);
      item = { ...item, state: payload.state, row_version: item.row_version + 1 };
      return json(route, item);
    }
    if (pathname.endsWith('/control-plans/failed-plan/retry') && request.method() === 'POST') {
      retried += 1;
      item = { ...item, row_version: item.row_version + 1, control_plans: [{ ...item.control_plans[0], id: 'retry-plan', state: 'PENDING', retry_of_plan_id: 'failed-plan', error_code: null }] };
      return json(route, item.control_plans[0], 201);
    }
    if (pathname.endsWith('/artifact-materials')) return json(route, { items: [{ id: 'material-1', kind: 'ARTIFACT', artifact_version_id: 'artifact-1', stage_key: 'review', execution_flow_run_id: 'execution', node_key: 'deliver', node_run_id: 'node', node_attempt_id: 'attempt', completion_event_id: 'completion', field_key: 'report', version_no: 2, artifact_type: 'URL', name: '交付报告', mime_type: null, byte_size: null, content_hash: null, state: 'ACCEPTED', candidate_status: 'GATE_PASSED', uri: 'https://example.test/report', content_url: null, registered_at: now, content_access: 'EXTERNAL_URI' }], next_offset: null });
    return json(route, pathname.endsWith('/cases') ? [item] : item);
  });
  await page.goto('/?view=cases');
  const stage = page.getByRole('article', { name: '路径卡片 质量复核' });
  await expect(stage).toContainText('等待 Agent 复核');
  await expect(stage).toContainText('正式产物 1');
  await expect(page.getByRole('status', { name: '场景协调状态' })).toContainText('结果已就绪，等待 Agent 判断');
  await expect(page.getByRole('alert')).toContainText('CASE_FEEDBACK_RETRIES_EXHAUSTED');
  await page.getByRole('button', { name: '暂停协调', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续协调', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '继续协调', exact: true }).click();
  expect(coordinationStates).toEqual(['PAUSED', 'ACTIVE']);
  await page.getByRole('button', { name: '查看异常与接管', exact: true }).click();
  const takeover = page.getByRole('dialog', { name: '异常与接管', exact: true });
  await expect(takeover).toContainText('结果符合目标，进入下一阶段');
  await takeover.getByRole('button', { name: '重试失败动作', exact: true }).click();
  expect(retried).toBe(1);
  await expect(takeover).toContainText('待处理');
});

test('manual Case stage retry retains its execution copy after a start failure', async ({ page }) => {
  await openWorkspace(page);
  let launches = 0, starts = 0;
  let item = { id: 'retry-case', name: '阶段重试', row_version: 1, state: 'ACTIVE', materials: [], tasks: [], control_plans: [], decision_proposals: [],
    stages: [{ id: 'stage', name: '交付阶段', flow_run_id: 'template', position: 0, state: 'ACTIVE', execution_mode: 'STEPWISE', current_execution_flow_run_id: null as string | null, execution: null as null | { state: string } }] };
  await page.route('**/api/v1/cases**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/launch')) {
      launches += 1; item = { ...item, row_version: 2, stages: [{ ...item.stages[0], current_execution_flow_run_id: 'execution', execution: { state: 'DRAFT' } }] };
      return json(route, item);
    }
    if (path.endsWith('/execution/start')) {
      starts += 1;
      if (starts === 1) return json(route, { error: { code: 'UNAVAILABLE', message: '暂不可启动' } }, 503);
      item = { ...item, row_version: 3, stages: [{ ...item.stages[0], execution: { state: 'ACTIVE' } }] };
      return json(route, { state: 'EXECUTED' });
    }
    return json(route, path.endsWith('/cases') ? [item] : item);
  });
  await page.getByRole('button', { name: '场景空间', exact: true }).click();
  await page.getByRole('button', { name: '手动启动', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂不可启动');
  await expect(page.getByRole('button', { name: '查看执行记录', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '手动启动', exact: true }).click();
  await expect(page.getByRole('article', { name: '路径卡片 交付阶段' })).toContainText('运行中');
  expect(launches).toBe(1); expect(starts).toBe(2);
});


test('schedule history retries bounded pages and ignores a late page after switching tasks', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 700 });
  await openWorkspace(page);
  const reads: Array<{ schedule: string; cursor: string | null }> = [];
  let rejectInitial = true, nextFailed = false;
  let releasePage: (() => void) | undefined;
  await page.route('**/conversation-schedules/*/run-conversations?*', async route => {
    const url = new URL(route.request().url());
    const schedule = url.pathname.split('/').at(-2)!;
    const cursor = url.searchParams.get('cursor');
    reads.push({ schedule, cursor });
    if (schedule === 'daily-schedule' && !cursor && rejectInitial) {
      return json(route, { error: { code: 'TEMPORARY', message: '暂不可用' } }, 503);
    }
    if (schedule === 'daily-schedule' && cursor && !nextFailed) {
      nextFailed = true;
      return json(route, { error: { code: 'TEMPORARY', message: '分页暂不可用' } }, 503);
    }
    if (schedule === 'daily-schedule' && cursor) await new Promise<void>(resolve => { releasePage = resolve; });
    const items = Array.from({ length: schedule === 'weekly-schedule' ? 1 : 20 }, (_, index) => ({
      id: `${schedule}-${cursor ?? 'head'}-${index}`, binding_id: `${schedule}-${cursor ?? 'head'}-${index}`,
      conversation_title: `${schedule} 会话 ${index}`, trigger_kind: 'SCHEDULED', state: 'STARTED', created_at: now,
    }));
    return json(route, { items, next_cursor: schedule === 'daily-schedule' && !cursor ? 'next-page' : null, total: schedule === 'daily-schedule' ? 40 : 1 });
  });
  await page.goto('/agent/schedules');
  await page.locator('.agent-schedule-tree-select').first().click();
  const detail = page.locator('.agent-schedule-detail');
  const list = detail.getByRole('region', { name: '运行会话列表' });
  await expect(list).toContainText('读取运行会话失败');
  rejectInitial = false;
  await list.getByRole('button', { name: '重试', exact: true }).click();
  await expect(list.locator('.agent-schedule-history-run')).toHaveCount(20);
  expect(reads.every(item => item.schedule === 'daily-schedule')).toBe(true);
  await list.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(list).toContainText('加载更多失败');
  await expect(list.locator('.agent-schedule-history-run')).toHaveCount(20);
  await list.getByRole('button', { name: '重试', exact: true }).click();
  await expect.poll(() => Boolean(releasePage)).toBe(true);
  await page.locator('.agent-schedule-tree-select').nth(1).click();
  await expect(detail.getByRole('heading', { name: '每周复核', exact: true })).toBeVisible();
  await expect(list.locator('.agent-schedule-history-run')).toHaveCount(1);
  releasePage?.();
  await expect(list).toContainText('weekly-schedule');
  await expect(list).not.toContainText('daily-schedule');
  await expect(page.locator('.agent-schedule-detail-main')).toHaveJSProperty('scrollTop', 0);
});


for (const mode of ['success', 'failure', 'missing-old-catalog'] as const) {
  test(`registered skill stages an inline update until save (${mode})`, async ({ page }) => {
    await openWorkspace(page);
    const old = { id: 'daily-v1', lineage_id: 'daily-lineage', capability_type: 'SKILL', capability_key: '日报技能', revision_number: 1, is_latest: false, description: '旧版日报规则', filename: 'SKILL.md' };
    const latest = { ...old, id: 'daily-v2', revision_number: 2, is_latest: true, description: '新版日报规则' };
    let active = old.id;
    let fail = mode === 'failure';
    const registrations: string[][] = [];
    const conversation = () => ({
      id: 'scheduled-conversation', lifecycle: 'ACTIVE', display_title: '日报会话',
      streaming_callback_ready: true, write_available: true, created_at: now, updated_at: now,
      capabilities: [{ id: active, capability_type: 'SKILL', capability_key: old.capability_key, digest: active }],
    });
    await page.route('**/api/v1/capabilities', route => json(route, mode === 'missing-old-catalog' ? [latest] : [old, latest]));
    await page.route('**/api/v1/capability-collections', route => json(route, [{ id: 'daily-collection', name: '日报组合', members: [latest] }]));
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/conversations/scheduled-conversation', route => json(route, conversation()));
    await page.route('**/capabilities/batch', route => {
      const ids = route.request().postDataJSON().capability_version_ids;
      registrations.push(ids);
      if (!fail) active = latest.id;
      return json(route, { conversation: conversation(), completed: !fail,
        loaded_capability_version_ids: fail ? [] : ids, already_loaded_capability_version_ids: [],
        failure: fail ? { capability_version_id: latest.id, code: 'PLUGIN_LOAD_FAILED', message: '新版加载失败' } : null });
    });
    await page.goto('/agent/conversations/scheduled-conversation');
    await page.getByRole('button', { name: '会话配置', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '会话配置' });
    const row = dialog.locator('[data-capability-key="日报技能"]');
    await expect(row).toHaveCount(1);
    await expect(row.getByText('有新版本', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '只看已选', exact: true }).click();
    await dialog.getByRole('button', { name: '日报组合', exact: true }).click();
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: '同步 日报技能 到 v2', exact: true }).click();
    await expect(row).toHaveCount(1);
    await expect(row.getByText('待保存', { exact: true })).toBeVisible();
    expect(registrations).toEqual([]);
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '会话配置', exact: true }).click();
    await expect(row.getByText('有新版本', { exact: true })).toBeVisible();
    expect(registrations).toEqual([]);
    await row.getByRole('button', { name: '同步 日报技能 到 v2', exact: true }).click();
    await page.setViewportSize({ width: 600, height: 800 });
    await expect(row.getByRole('button', { name: '同步 日报技能 到 v2', exact: true })).toBeInViewport();
    await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeInViewport();
    await dialog.screenshot({ path: `../../.tmp/ops46-skill-update-${mode}.png` });
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    if (fail) {
      await expect(dialog.getByText(/新版加载失败/)).toBeVisible();
      await expect(row).toHaveCount(1);
      await expect(row.getByText('待保存', { exact: true })).toBeVisible();
      fail = false;
      await dialog.getByRole('button', { name: '保存', exact: true }).click();
    }
    await expect(dialog).toHaveCount(0);
    expect(registrations).toEqual(Array.from({ length: mode === 'failure' ? 2 : 1 }, () => ['daily-v2']));
    await page.getByRole('button', { name: '会话配置', exact: true }).click();
    await expect(row).toHaveCount(1);
    await expect(row.locator('b')).toHaveText('日报技能 · v2');
    await expect(row.getByText('有新版本', { exact: true })).toHaveCount(0);
  });
}


test('session configuration keeps compact capability rows and one-line credential summaries', async ({ page }) => {
  await openWorkspace(page);
  const types = ['SKILL', 'PLUGIN', 'MCP', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK'];
  const catalog = types.map(type => ({ id: `compact-${type}`, capability_type: type,
    capability_key: `${type} 名称${'很长的名称'.repeat(12)}`, description: '说明内容'.repeat(50), filename: 'entry.md', is_latest: true }));
  await page.route('**/api/v1/capabilities', route => json(route, catalog));
  await page.getByRole('button', { name: '新会话配置', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '会话配置' });
  const assertRow = async (row: import('@playwright/test').Locator) => {
    const layout = await row.evaluate(element => {
      const mark = element.querySelector<HTMLElement>('.agent-selection-checkbox')!;
      const icon = element.querySelector<HTMLElement>('.agent-capability-icon')!;
      const title = element.querySelector<HTMLElement>('b')!;
      const description = element.querySelector<HTMLElement>('small')!;
      const rect = element.getBoundingClientRect(), m = mark.getBoundingClientRect(), i = icon.getBoundingClientRect(), t = title.getBoundingClientRect();
      const style = getComputedStyle(mark);
      return { height: rect.height, markSize: [m.width, m.height], radius: style.borderRadius,
        ordered: m.right <= i.left && i.right <= t.left, titleLine: title.getBoundingClientRect().height,
        descriptionLine: description.getBoundingClientRect().height, descriptionWrap: getComputedStyle(description).whiteSpace,
        overflow: element.scrollWidth > element.clientWidth,
        icon: icon.querySelector('svg')!.getAttribute('class') };
    });
    expect(layout.height).toBeLessThanOrEqual(50);
    expect(layout.markSize).toEqual([16, 16]);
    expect(layout.radius).toBe('4px');
    expect(layout.ordered).toBe(true);
    expect(layout.titleLine).toBeLessThanOrEqual(16);
    expect(layout.descriptionLine).toBeLessThanOrEqual(15);
    expect(layout.descriptionWrap).toBe('nowrap');
    expect(layout.overflow).toBe(false);
    return layout.icon;
  };
  for (const width of [1440, 600]) {
    await page.setViewportSize({ width, height: 900 });
    const icons = [];
    for (const item of catalog) {
      const row = dialog.locator('.agent-capability-list > button').filter({ has: page.locator('b', { hasText: item.capability_key }) });
      icons.push(await assertRow(row));
    }
    expect(new Set(icons).size).toBe(types.length);
    await dialog.screenshot({ path: `../../.tmp/ops47-capabilities-${width}.png` });
  }
  const skill = dialog.locator('.agent-capability-list > button').filter({ has: page.locator('b', { hasText: catalog[0].capability_key }) });
  await skill.locator('.agent-selection-checkbox').click();
  await expect(skill).toHaveAttribute('aria-pressed', 'true');
  await skill.focus();
  await page.keyboard.press('Space');
  await expect(skill).toHaveAttribute('aria-pressed', 'false');
  await dialog.getByRole('button', { name: '认证', exact: true }).click();
  const credential = dialog.locator('.agent-credential-sync .agent-capability-list > button').first();
  const credentialLayout = await credential.evaluate(element => {
    const title = element.querySelector<HTMLElement>('.agent-credential-copy b')!;
    const summary = element.querySelector<HTMLElement>('.agent-credential-copy small')!;
    const icon = element.querySelector('.agent-capability-icon svg')!.getAttribute('class');
    const row = element.getBoundingClientRect();
    const titleBox = title.getBoundingClientRect(), summaryBox = summary.getBoundingClientRect();
    return { height: row.height, aligned: Math.abs(titleBox.top - summaryBox.top) < 1, summaryWrap: getComputedStyle(summary).whiteSpace, icon };
  });
  expect(credentialLayout.height).toBeLessThanOrEqual(46);
  expect(credentialLayout.aligned).toBe(true);
  expect(credentialLayout.summaryWrap).toBe('nowrap');
  expect(credentialLayout.icon).toContain('lucide-key-round');
  await expect(credential).toHaveAttribute('aria-pressed', 'true');
  await credential.locator('.agent-selection-checkbox').click();
  await expect(credential).toHaveAttribute('aria-pressed', 'false');
  const heading = dialog.locator('.agent-credential-sync-heading');
  expect(await heading.evaluate(element => {
    const select = element.querySelector('button')!.getBoundingClientRect();
    const name = element.querySelector('span')!.getBoundingClientRect();
    return select.right < name.left;
  })).toBe(true);
  await dialog.getByRole('button', { name: '全选认证', exact: true }).click();
  await expect(credential).toHaveAttribute('aria-pressed', 'true');
  await dialog.screenshot({ path: '../../.tmp/ops47-credentials.png' });
});

test('workspace file review shares exact intraline highlighting in unified and split modes', async ({ page }) => {
  await openWorkspace(page, [
    { id: 'diff-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '更新配置', timestamp: now } },
    { id: 'diff-action', event_type: 'TOOL_CALL', payload: { parent_id: 'diff-user', action_id: 'diff-action', tool_call_id: 'diff-call', tool_name: 'file_editor', event_name: 'FileEditorAction', details: { command: 'str_replace', path: '/runtime/workspace/project/config.ts', old_str: 'const mode = "old"', new_str: 'const mode = "new"' }, timestamp: now } },
    { id: 'diff-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'diff-action', action_id: 'diff-action', tool_call_id: 'diff-call', tool_name: 'file_editor', event_name: 'FileEditorObservation', content: 'The file was edited successfully.', details: { command: 'str_replace', path: '/runtime/workspace/project/config.ts', is_error: false }, timestamp: now } },
    { id: 'diff-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'diff-result', content: '配置已更新。', final_reply: true, timestamp: now } },
  ]);
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '已编辑 1 个文件' }).click();
  const diff = page.locator('.agent-changes-review');
  for (const mode of ['统一', '并排']) {
    await diff.getByRole('button', { name: mode, exact: true }).click();
    await expect(diff.locator('.addition .hljs-string .agent-diff-inline-change')).toHaveText('new');
    await expect(diff.locator('.deletion .hljs-string .agent-diff-inline-change')).toHaveText('old');
    await expect(diff.locator('.addition code')).toHaveText('const mode = "new"');
    await expect(diff.locator('.deletion code')).toHaveText('const mode = "old"');
  }
});

async function expectInsideViewport(page: Page, popover: import('@playwright/test').Locator) {
  await expect(popover).toBeVisible();
  await expect.poll(async () => {
    const box = await popover.boundingBox();
    const viewport = page.viewportSize()!;
    return Boolean(box && box.width > 0 && box.height > 0 && box.x >= 7 && box.y >= 7 && box.x + box.width <= viewport.width - 7 && box.y + box.height <= viewport.height - 7);
  }).toBe(true);
}

test('workspace action popovers flip above and left and follow resize and content growth', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await openWorkspace(page);
  const trigger = page.getByRole('button', { name: '工作区操作 已有项目目录', exact: true });
  // Put the real trigger at the screenshot's boundary, independent of sidebar row count.
  await trigger.evaluate(element => Object.assign(element.style, { position: 'fixed', right: '12px', bottom: '12px', opacity: '1', zIndex: '2100' }));
  await trigger.dispatchEvent('click');
  const menu = page.getByRole('menu', { name: '工作区操作菜单：已有项目目录', exact: true });
  await expectInsideViewport(page, menu);
  const anchor = (await trigger.boundingBox())!;
  const box = (await menu.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(anchor.y);
  expect(box.x).toBeLessThan(anchor.x);
  await page.setViewportSize({ width: 760, height: 420 });
  await expectInsideViewport(page, menu);
  const resizedAnchor = (await trigger.boundingBox())!;
  await expect.poll(async () => { const current = (await menu.boundingBox())!; return current.y + current.height <= resizedAnchor.y; }).toBe(true);
  await page.screenshot({ path: test.info().outputPath('workspace-menu-bottom-right.png') });
  // A changing action list must be measured again and remain scrollable at short heights.
  await menu.evaluate(element => {
    for (let i = 0; i < 20; i++) {
      const button = document.createElement('button'); button.textContent = `额外操作 ${i + 1}`; button.setAttribute('role', 'menuitem'); element.append(button);
    }
  });
  await expectInsideViewport(page, menu);
  await expect.poll(() => menu.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  await menu.getByRole('menuitem', { name: '额外操作 20', exact: true }).scrollIntoViewIfNeeded();
  await expect(menu.getByRole('menuitem', { name: '额外操作 20', exact: true })).toBeInViewport();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  await trigger.dispatchEvent('click');
  await menu.getByRole('menuitem', { name: '会话配置', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '会话配置', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('conversation context menu measures all actions at each viewport corner', async ({ page }) => {
  await openWorkspace(page);
  const row = page.locator('[data-conversation-binding-id="scheduled-conversation"]');
  const menu = page.getByRole('menu', { name: '会话操作菜单：日报会话', exact: true });
  for (const corner of ['bottom-right', 'top-left'] as const) {
    await row.evaluate((element, value) => Object.assign((element as HTMLElement).style, { position: 'fixed', left: value === 'top-left' ? '0' : 'auto', top: value === 'top-left' ? '0' : 'auto', right: value === 'bottom-right' ? '0' : 'auto', bottom: value === 'bottom-right' ? '0' : 'auto', width: '180px', height: '32px', zIndex: '1800' }), corner);
    await row.click({ button: 'right', position: corner === 'bottom-right' ? { x: 178, y: 30 } : { x: 1, y: 1 } });
    await expectInsideViewport(page, menu);
    await expect(menu.getByRole('menuitem', { name: '标记为未读', exact: true })).toBeVisible();
    await expect(menu.getByRole('menuitem', { name: '删除', exact: true })).toHaveCSS('justify-content', 'flex-start');
    if (corner === 'bottom-right') {
      const box = (await menu.boundingBox())!; const viewport = page.viewportSize()!;
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 2);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 2);
    }
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);
  }
  await page.setViewportSize({ width: 260, height: 160 });
  await row.evaluate(element => Object.assign((element as HTMLElement).style, { left: 'auto', top: 'auto', right: '0', bottom: '0' }));
  await row.click({ button: 'right', position: { x: 178, y: 30 } });
  await expectInsideViewport(page, menu);
  await menu.getByRole('menuitem', { name: '标记为未读', exact: true }).click();
  await expect(menu).toHaveCount(0);
});

test('account action popover stays inside a short viewport and closes outside', async ({ page }) => {
  await openWorkspace(page);
  await page.setViewportSize({ width: 680, height: 240 });
  const trigger = page.getByRole('button', { name: '账户与设置', exact: true });
  await trigger.click();
  const menu = page.getByRole('menu', { name: '账户与设置', exact: true });
  await expectInsideViewport(page, menu);
  await menu.getByRole('menuitem', { name: '退出登录', exact: true }).scrollIntoViewIfNeeded();
  await expect(menu.getByRole('menuitem', { name: '退出登录', exact: true })).toBeInViewport();
  await page.mouse.click(12, 210);
  await expect(menu).toHaveCount(0);
  await trigger.click();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
});

test('flow action popover escapes a clipped list and keeps selection and actions independent', async ({ page }) => {
  await openWorkspace(page);
  await page.route('**/api/v1/flows', route => json(route, [{ id: 'popover-flow', name: '边界流程', description: '', nodes: [], edges: [], created_at: now, updated_at: now }]));
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  const trigger = page.getByRole('button', { name: '操作流程 边界流程', exact: true });
  await trigger.click();
  const menu = page.getByRole('menu', { name: '流程操作菜单：边界流程', exact: true });
  await expectInsideViewport(page, menu);
  const checkbox = page.getByRole('checkbox', { name: '选择流程 边界流程', exact: true });
  await expect(checkbox).not.toBeChecked();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  await trigger.click();
  await menu.getByRole('button', { name: '进入编排', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存编排', exact: true })).toBeVisible();
  await expect(menu).toHaveCount(0);
});

test('queued message action popover preserves upward placement outside dismissal and editing', async ({ page }) => {
  await openWorkspace(page);
  await page.evaluate(() => {
    sessionStorage.setItem('flowweave.agent.message-queue.v1:schedule-workspace:scheduled-conversation', JSON.stringify([{ id: 'edge-message', scope: 'scheduled-conversation', content: '边界排队消息', items: [], references: [], annotations: [], deliveryState: 'queued' }]));
    sessionStorage.setItem('flowweave.agent.queue-mode.v1:schedule-workspace:scheduled-conversation', 'disabled');
  });
  await page.goto('/agent/conversations/scheduled-conversation');
  const trigger = page.getByRole('button', { name: '更多排队消息操作 1', exact: true });
  await trigger.click();
  const menu = page.getByRole('menu', { name: '排队消息操作', exact: true });
  await expectInsideViewport(page, menu);
  const anchor = (await trigger.boundingBox())!; const box = (await menu.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(anchor.y);
  await expect(menu.getByRole('menuitem', { name: '编辑消息', exact: true })).toHaveCSS('border-top-width', '0px');
  await page.getByRole('textbox', { name: '发送 Agent 消息' }).click();
  await expect(menu).toHaveCount(0);
  await trigger.click();
  await menu.getByRole('menuitem', { name: '编辑消息', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '发送 Agent 消息' })).toHaveValue('边界排队消息');
  await expect(menu).toHaveCount(0);
});

test('file action popover measures directory actions at the bottom right', async ({ page }) => {
  await openWorkspace(page);
  const root = '/runtime/workspace/project';
  await page.route('**/api/v1/agent-workspaces/*/workspace/directory**', route => json(route, { parent_path: root, entries: [{ path: `${root}/src`, kind: 'directory', size: 0 }], next_cursor: null }));
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '文件', exact: true }).click();
  const viewport = page.viewportSize()!;
  await page.getByRole('button', { name: 'src', exact: true }).evaluate((element, viewport) => element.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2, clientX: viewport.width - 2, clientY: viewport.height - 2 })), viewport);
  const menu = page.getByRole('menu', { name: '文件操作菜单', exact: true });
  await expectInsideViewport(page, menu);
  await expect(menu.getByRole('menuitem')).toHaveCount(3);
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
});

test('terminal action popover measures its full command list at the bottom right', async ({ page }) => {
  await openWorkspace(page);
  await page.routeWebSocket('**/*terminal*', socket => socket.send('terminal ready\r\n'));
  await page.goto('/agent/conversations/scheduled-conversation');
  await page.getByRole('button', { name: '新建工作区页签', exact: true }).first().click();
  await page.locator('.agent-workspace-tab-launcher.active').getByRole('button', { name: '终端', exact: true }).click();
  const terminal = page.locator('.agent-workspace-terminal');
  await expect(terminal).toContainText('已连接');
  const viewport = page.viewportSize()!;
  await terminal.locator('.xterm-screen').dispatchEvent('mousedown', { button: 2, clientX: viewport.width - 2, clientY: viewport.height - 2 });
  const menu = page.getByRole('menu', { name: '终端操作菜单', exact: true });
  await expectInsideViewport(page, menu);
  await expect(menu.getByRole('menuitem', { name: '关闭当前窗格', exact: true })).toBeInViewport();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
});

for (const archiveTarget of ['conversation', 'directory', 'rejected-directory'] as const) {
  test(`workspace archive immediately reconciles loaded pages pinned and activity lists (${archiveTarget})`, async ({ page }) => {
    await openWorkspace(page);
    const removedIds = new Set<string>();
    let directoryArchived = false;
    let archiveWrites = 0;
    const items = Array.from({ length: 6 }, (_, index) => ({
      id: `archive-child-${index}`, display_title: `归档验证会话 ${index}`, title_state: 'MANUAL', lifecycle: 'ACTIVE',
      work_directory_id: 'existing-directory', pinned: index === 0, unread: true,
      streaming_callback_ready: true, write_available: true, execution_status: 'idle',
      created_at: now, updated_at: now,
    }));
    const root = { ...items[0], id: 'archive-root', display_title: '保留根会话', work_directory_id: null, pinned: false, unread: false };
    const live = () => items.filter(item => !removedIds.has(item.id));
    await page.route('**/api/v1/agent-workspaces/schedule-workspace/**', async route => {
      const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
      if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: directoryArchived && url.searchParams.get('include_archived') !== 'true' ? [] : [{ id: 'existing-directory', display_name: '已有项目目录', state: directoryArchived ? 'ARCHIVED' : 'ACTIVE', current_version: { id: 'archive-version', working_directory: '/runtime/workspace/project/existing-directory' } }] });
      if (path.endsWith('/work-directories/existing-directory/restore')) {
        directoryArchived = false; removedIds.clear();
        return json(route, { id: 'existing-directory', display_name: '已有项目目录', state: 'ACTIVE' });
      }
      if (path.endsWith('/conversations/archive-child-5/restore')) {
        directoryArchived = false; removedIds.delete('archive-child-5');
        return json(route, { ...items[5], archived_at: null });
      }
      if (path.endsWith('/conversation-management')) return json(route, { items: url.searchParams.get('work_directory_id') === 'existing-directory' ? items.filter(item => removedIds.has(item.id) === (url.searchParams.get('archived') === 'true')).map(item => ({ ...item, archived_at: removedIds.has(item.id) ? now : null })) : [], truncated: false });
      if (path.endsWith('/pinned-conversations')) return json(route, live().filter(item => item.pinned));
      if (path.endsWith('/conversation-activity')) return json(route, { running_binding_ids: [], unread_binding_ids: live().map(item => item.id), conversations: live() });
      if (path.endsWith('/conversations') && request.method() === 'GET') {
        if (url.searchParams.get('work_directory_id') !== 'existing-directory') return json(route, { items: [root], next_cursor: null });
        const offset = url.searchParams.has('cursor') ? 3 : 0;
        return json(route, { items: live().slice(offset, offset + 3), next_cursor: offset === 0 && live().length > 3 ? 'archive-page-2' : null });
      }
      if (path.endsWith('/work-directories/existing-directory/archive')) {
        archiveWrites += 1;
        if (archiveTarget === 'rejected-directory') return json(route, { error: { code: 'AGENT_CONVERSATION_RUNNING', message: '会话正在运行，请先停止' } }, 409);
        directoryArchived = true; items.forEach(item => removedIds.add(item.id));
        return json(route, { id: 'existing-directory', display_name: '已有项目目录', state: 'ARCHIVED' });
      }
      if (path.endsWith('/archive') && request.method() === 'POST') {
        archiveWrites += 1;
        const id = path.match(/\/conversations\/([^/]+)/)?.[1];
        removedIds.add(id!);
        return json(route, { ...items.find(item => item.id === id), archived_at: now });
      }
      const detail = [root, ...items].find(item => path.endsWith(`/conversations/${item.id}`));
      if (detail) return json(route, detail);
      return route.fallback();
    });
    await page.reload();
    const group = page.locator('[data-workspace-group-id="existing-directory"]');
    // The sixth item is from a later server page. A first-page refetch alone
    // cannot remove it from the workbench's retained per-scope pages.
    await page.getByRole('button', { name: '归档验证会话 5', exact: true }).click();
    await expect(page).toHaveURL(/\/archive-child-5$/);
    await page.getByRole('button', { name: /查看活动会话/ }).click();
    await expect(page.getByRole('button', { name: '归档验证会话 0', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '返回工作区列表', exact: true }).click();
    if (archiveTarget === 'conversation') {
      await page.locator('[data-conversation-binding-id="archive-child-5"]').hover();
      await page.getByRole('button', { name: '归档会话 归档验证会话 5', exact: true }).click();
      await expect(page.locator('[data-conversation-binding-id="archive-child-5"]')).toHaveCount(0);
      await page.locator('[data-conversation-binding-id="archive-child-0"]').hover();
      await page.getByRole('button', { name: '归档会话 归档验证会话 0', exact: true }).click();
      await expect(page.locator('[data-conversation-binding-id="archive-child-0"]')).toHaveCount(0);
    } else {
      await group.locator('header').hover();
      await page.getByRole('button', { name: '工作区操作 已有项目目录', exact: true }).click();
      await page.getByRole('menuitem', { name: '归档', exact: true }).click();
      await expect.poll(() => archiveWrites).toBe(1);
      if (archiveTarget === 'rejected-directory') {
        await expect(page.getByText('会话正在运行，请先停止', { exact: false })).toBeVisible();
        await expect(group).toBeVisible();
        await expect(group.getByRole('button', { name: '归档验证会话 5', exact: true })).toBeVisible();
        return;
      }
      await expect(group).toHaveCount(0);
      await expect(page.locator('[data-conversation-binding-id^="archive-child-"]')).toHaveCount(0);
    }
    await expect(page.getByRole('button', { name: '保留根会话', exact: true })).toBeVisible();
    await expect(page).not.toHaveURL(/\/archive-child-5$/);
    await page.getByRole('button', { name: /查看活动会话/ }).click();
    for (const id of removedIds) await expect(page.locator(`[data-conversation-binding-id="${id}"]`)).toHaveCount(0);
    if (archiveTarget === 'directory') await expect(page.getByText('没有正在运行或未读的会话。', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '返回工作区列表', exact: true }).click();
    await page.screenshot({ path: test.info().outputPath(`archived-${archiveTarget}.png`) });
    if (archiveTarget === 'directory') {
      // Management lives inside the same mounted workbench: a restore must
      // clear successful-archive exclusions as well as refetch server lists.
      await page.getByRole('button', { name: '会话管理', exact: true }).click();
      await page.getByRole('button', { name: '已有项目目录（已归档）', exact: true }).click();
      await page.getByRole('button', { name: '恢复工作区 已有项目目录', exact: true }).click();
      await page.getByRole('alertdialog').getByRole('button', { name: '确认恢复', exact: true }).click();
      await expect(page.getByRole('button', { name: '已有项目目录', exact: true })).toBeVisible();
      await page.getByRole('button', { name: '主页', exact: true }).click();
      await expect(group).toBeVisible();
      await expect(page.getByRole('button', { name: '归档验证会话 0', exact: true })).toBeVisible();
      await group.locator('header').hover();
      await page.getByRole('button', { name: '工作区操作 已有项目目录', exact: true }).click();
      await page.getByRole('menuitem', { name: '归档', exact: true }).click();
      await expect(group).toHaveCount(0);
      await page.getByRole('button', { name: '会话管理', exact: true }).click();
      await page.getByRole('button', { name: '已有项目目录（已归档）', exact: true }).click();
      await page.getByRole('checkbox', { name: '包含未读', exact: true }).check();
      await page.getByRole('button', { name: '恢复会话 归档验证会话 5', exact: true }).click();
      await expect(page.getByRole('button', { name: '已有项目目录', exact: true })).toBeVisible();
      await page.getByRole('button', { name: '主页', exact: true }).click();
      await expect(group.getByRole('button', { name: '归档验证会话 5', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '归档验证会话 0', exact: true })).toHaveCount(0);
    }

  });
}
