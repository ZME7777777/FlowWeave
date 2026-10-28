import { expect, test } from '@playwright/test';

const now = '2026-09-01T00:00:00Z';
const authenticatedUser = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'flowweave',
  role: 'SUPER_ADMIN',
  is_super_admin: true,
};
const asset = {
  id: 'asset-1', name: '测试节点', description: '', icon_kind: 'LUCIDE', icon_value: 'bot', row_version: 1,
  inputs: [
    { field_key: 'input_1', display_name: 'input_1', data_type: 'URL', description: '' },
    { field_key: 'input_2', display_name: 'input_2', data_type: 'FILE', description: '' },
  ],
  outputs: [{ field_key: 'output_1', display_name: 'output_1', data_type: 'URL', description: '' }],
  executor: { startup_prompt: '读取流程输入并完成节点工作。', context_prompt: '', context_capability_ids: [] },
  context_capabilities: [], created_at: now, updated_at: now,
};
const definition = {
  id: 'flow-1', name: '测试流程', description: '', default_entry_key: 'first', row_version: 1,
  nodes: [
    { id: 'flow-node-1', instance_key: 'first', node_asset_id: asset.id, alias: '测试节点', position_x: 80, position_y: 120, config_override: {}, gates: [], asset },
    { id: 'flow-node-2', instance_key: 'second', node_asset_id: asset.id, alias: '测试节点2', position_x: 480, position_y: 120, config_override: {}, gates: [], asset: { ...asset, id: 'asset-2', name: '测试节点2' } },
  ],
  edges: [{ id: 'edge-1', source_instance_key: 'first', target_instance_key: 'second', position: 0 }],
  port_mappings: [{ id: 'mapping-1', source_instance_key: 'first', source_output_key: 'output_1', target_instance_key: 'second', target_input_key: 'input_1' }],
  created_at: now, updated_at: now,
};
const snapshot = {
  id: 'snapshot-1', version: 1, schema_version: 2, definition_hash: '72a80424abcdef',
  environment_version_id: 'environment-version-1', definition, created_at: now,
};
const attempt = {
  id: 'attempt-1', node_run_id: 'node-run-1', attempt_no: 1, snapshot_id: snapshot.id,
  state: 'EXECUTING', state_version: 1, runtime_cancel_recovery_modes: [], startup_mode: 'PROMPT',
  startup_prompt: '读取流程输入并完成节点工作。', context_ids: [],
  agent_preset: { capability_version_ids: [], node_context_enabled: false }, gate_policies: [],
  input_bindings: [], artifacts: [], gate_evaluations: [], runtime_confirmation_batches: [],
  created_at: now, updated_at: now,
};
const nodeRun = {
  id: 'node-run-1', flow_run_id: 'run-1', flow_node_snapshot_key: 'first', sequence_no: 1,
  state: 'ACTIVE', created_from: 'MANUAL', activated_at: now, attempts: [attempt],
};
const run = {
  id: 'run-1', flow_definition_id: definition.id, flow_name: definition.name, flow_row_version: 1,
  run_no: 1, name: '测试运行', state: 'ACTIVE', run_mode: 'MANUAL', completion_mode: null,
  row_version: 1, active_snapshot_id: snapshot.id, active_snapshot_version: 1,
  environment_version_id: 'environment-version-1', environment_version: null,
  current_node_key: 'first', current_node_name: '测试节点', current_attempt_state: 'EXECUTING',
  has_pending_action: false, runtime_status: 'ACTIVE', runtime_write_available: true, runtime_message: null,
  lark_folder_token: null, lark_folder_url: null, progress: { accepted: 0, terminal: 0, active: 1 },
  snapshots: [snapshot], node_runs: [nodeRun], artifacts: [], started_at: now, updated_at: now, finished_at: null,
};
const automaticBase = {
  ...run, id: 'automatic-1', run_no: 2, name: '自动记录 1', state: 'DRAFT', run_mode: 'AUTOMATIC',
  row_version: 1, parent_flow_run_id: run.id, runtime_status: 'DRAFT', runtime_write_available: false,
  current_node_key: null, current_node_name: null, current_attempt_state: null,
  progress: { accepted: 0, terminal: 0, active: 0 }, node_runs: [], artifacts: [],
  automation_plan: {
    status: 'DRAFT', start_node_key: 'first', reachable_node_keys: ['first', 'second'], node_plans: {},
    readiness: { ready: false, issues: [
      { code: 'NODE_PLAN_REQUIRED', node_key: 'first', message: '请配置此节点的自动执行预设' },
      { code: 'NODE_PLAN_REQUIRED', node_key: 'second', message: '请配置此节点的自动执行预设' },
    ] },
  },
};


type DirectoryFixture = { id: string; name: string; state: string; started_at: string; start_node_key?: string; schedule_id?: string; schedule_name?: string; progress?: { accepted: number }; plan?: { reachable_node_count: number }; automation_plan?: { reachable_node_keys: string[] } };
function directoryFixture(manual: DirectoryFixture[], automatic: DirectoryFixture[], url: string) {
  const query = new URL(url).searchParams;
  const kind = query.get('kind') ?? 'ALL';
  const term = (query.get('q') ?? '').trim().toLowerCase();
  const offset = Number(query.get('cursor') ?? 0);
  const limit = Number(query.get('limit') ?? 30);
  const items = [
    ...manual.map(item => ({ ...item, kind: 'MANUAL', detail: item.start_node_key ? '逐步配置与确认执行' : '等待配置起始节点' })),
    ...automatic.map(item => ({ ...item, kind: 'AUTOMATIC', detail: `${item.progress?.accepted ?? 0}/${item.plan?.reachable_node_count ?? item.automation_plan?.reachable_node_keys.length ?? 0} 节点完成` })),
  ].filter(item => kind === 'ALL' || item.kind === kind).filter(item => !term || item.name.toLowerCase().includes(term) || item.id.toLowerCase().includes(term)).sort((a, b) => b.started_at.localeCompare(a.started_at) || b.id.localeCompare(a.id));
  return { items: items.slice(offset, offset + limit), next_cursor: offset + limit < items.length ? String(offset + limit) : null };
}

test('record directory groups scheduled runs and opens a full canvas workspace', async ({ page }) => {
  const manual = { ...run, id: 'manual-record', name: '逐步验收记录', parent_flow_run_id: run.id, start_node_key: 'first' };
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flow-runs/${manual.id}`) return respond(manual);
    if (path.endsWith('/stepwise-runs/manual-record')) return respond(manual);
    if (path.endsWith('/records')) return respond(directoryFixture([manual], [{ ...automaticBase, plan: { reachable_node_count: 2 } }, { ...automaticBase, id: 'scheduled-1', name: '定时记录 1', schedule_id: 'schedule-1', schedule_name: '每日验收', plan: { reachable_node_count: 2 } }], route.request().url()));
    if (path.endsWith('/stepwise-runs')) return respond([manual]);
    if (path.endsWith('/automatic-runs/summaries')) return respond([
      { ...automaticBase, plan: { ...automaticBase.automation_plan, reachable_node_count: 2 } },
      { ...automaticBase, id: 'scheduled-1', name: '定时记录 1', schedule_id: 'schedule-1', schedule_name: '每日验收', plan: { ...automaticBase.automation_plan, reachable_node_count: 2 } },
    ]);
    if (path.endsWith('/automatic-runs/automatic-1')) return respond(automaticBase);
    if (path.endsWith('/runtime/resource')) return respond({ resource: null });
    if (path.endsWith('/runtime')) return respond({ status: 'ACTIVE', active_generation: 1, write_available: false });
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await expect(page.locator('.run-directory-empty')).toContainText('选择一个流程编排');
  await page.locator('.flow-run-tree-select').filter({ hasText: definition.name }).click();
  await expect(page.locator('.run-directory-empty')).toContainText(definition.name);
  await expect(page.locator('.flow-run-tree-toggle')).toHaveCount(0);
  const folder = page.locator('.flow-run-tree-select');
  await folder.click();
  await expect(folder).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('.flow-run-tree-run')).toHaveCount(0);
  await folder.click();
  await expect(folder).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.flow-run-tree-run')).toBeVisible();
  await expect(page.locator('.flow-run-tree-run .run-runtime-badge')).toHaveText('容器运行中');
  await expect(page.locator('.flow-run-tree-run')).not.toContainText('等待人工');
  await page.locator('.flow-run-tree-run').filter({ hasText: run.name }).click();
  const directory = page.locator('.flow-run-record-directory');
  await expect(directory.locator('.flow-run-record-row')).toHaveCount(3);
  await expect(directory.getByRole('button', { name: '全选', exact: true })).toBeVisible();
  await expect(directory.getByRole('button', { name: '拷贝', exact: true })).toBeDisabled();
  const manualRecord = directory.getByRole('checkbox', { name: `选择运行记录 ${manual.name}`, exact: true });
  await manualRecord.click();
  await expect(manualRecord).toBeChecked();
  await expect(directory).toBeVisible();
  await expect(directory.getByText('已选择 1 条')).toBeVisible();
  await expect(directory.getByRole('button', { name: '拷贝', exact: true })).toBeEnabled();
  for (const width of [1920, 1440, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    const rows = await directory.locator('.flow-run-record-row').evaluateAll(elements => elements.map(element => ({ height: element.getBoundingClientRect().height, overflow: element.scrollWidth - element.clientWidth })));
    expect(rows.every(row => row.height <= 50 && row.overflow <= 1)).toBe(true);
    await expect(directory.getByRole('button', { name: `进入运行记录 ${manual.name}`, exact: true }).getByText('打开', { exact: true })).toBeVisible();
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await directory.screenshot({ path: '../../.tmp/ops20/record-directory.png' });
  await directory.getByRole('button', { name: '连续', exact: true }).click();
  await expect(directory.locator('.flow-run-record-directory-list>.flow-run-record-row')).toHaveCount(1);
  const group = directory.locator('.flow-run-schedule-directory');
  const toggle = group.getByRole('button', { name: /每日验收/ });
  await expect(group.getByRole('button', { name: '进入运行记录 定时记录 1', exact: true })).toBeVisible();
  await expect(group.locator('.flow-run-record-row')).toHaveCSS('display', 'grid');
  await page.screenshot({ path: '../../.tmp/ui-696/scheduled-records.png' });
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(group.getByRole('button', { name: '进入运行记录 定时记录 1', exact: true })).toHaveCount(0);
  await expect(directory.locator('.flow-run-record-directory-list>.flow-run-record-row')).toHaveCount(1);
  await toggle.click();
  await expect(group.getByRole('button', { name: '进入运行记录 定时记录 1', exact: true })).toBeVisible();
  const automaticContent = directory.getByRole('button', { name: `选择运行记录 ${automaticBase.name}`, exact: true });
  await automaticContent.getByText(automaticBase.name, { exact: true }).click();
  await expect(automaticContent).toHaveAttribute('aria-pressed', 'true');
  await expect(directory).toBeVisible();
  await expect(directory.getByRole('checkbox', { name: `选择运行记录 ${automaticBase.name}`, exact: true })).toBeChecked();
  await expect(directory.getByText('已选择 1 条')).toBeVisible();
  await automaticContent.press('Enter');
  await expect(automaticContent).toHaveAttribute('aria-pressed', 'false');
  await expect(directory).toBeVisible();
  await directory.getByRole('button', { name: `进入运行记录 ${automaticBase.name}`, exact: true }).click();
  await expect(page.locator('.flow-run-workbench-header h1')).toHaveText(automaticBase.name);
  await expect(page.locator('.run-graph')).toBeVisible();
  await expect(page.locator('.run-record-detail-panel')).toBeVisible();
  await expect(page.locator('.run-record-detail-panel')).toHaveCSS('grid-column-start', '1');
  await expect(page.locator('.run-record-main')).toHaveCSS('grid-column-start', '2');
  await expect(page.getByRole('button', { name: `启动连续运行 ${automaticBase.name}` })).toHaveCount(0);
  await page.locator('.react-flow__pane').click({ position: { x: 15, y: 15 } });
  await expect(page.locator('.flow-run-workbench-header h1')).toHaveText(automaticBase.name);
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    const sidebar = await page.locator('.run-record-detail-panel').boundingBox();
    const canvas = await page.locator('.run-record-main').boundingBox();
    expect(sidebar!.x).toBe(0);
    expect(canvas!.x).toBe(sidebar!.width);
    expect(canvas!.x + canvas!.width).toBe(width);
    await page.screenshot({ path: `../../.tmp/ui-696/record-workspace-${width}.png` });
  }
  await expect(page.locator('.flow-run-inner-rail')).toHaveCount(0);
  await expect(page.getByRole('separator', { name: '调整运行记录栏宽度' })).toHaveCount(0);
  await page.getByRole('button', { name: '返回运行记录', exact: true }).click();
  await expect(page.locator('.flow-run-tree-select').filter({ hasText: definition.name })).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.flow-run-tree-run').filter({ hasText: run.name })).toHaveClass(/active/);
  await expect(directory.getByRole('checkbox', { name: `选择运行记录 ${automaticBase.name}`, exact: true })).not.toBeChecked();
  await directory.getByRole('button', { name: '逐步', exact: true }).click();
  await expect(manualRecord).toBeChecked();
  await directory.getByRole('button', { name: `进入运行记录 ${manual.name}`, exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.flow-run-workbench-header h1')).toHaveText(manual.name);
  await expect(page.locator('.run-graph')).toBeVisible();
});

test('run directory pauses and restores the selected container', async ({ page }) => {
  const requests: string[] = [];
  let runtimeStatus: 'ACTIVE' | 'STOPPED' = 'ACTIVE';
  let sessionRowVersion = 1;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([{ ...run, runtime_status: runtimeStatus, runtime_write_available: runtimeStatus === 'ACTIVE' }]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path === `/api/v1/flow-runs/${run.id}/runtime`) return respond({ active_generation: 1, session_row_version: sessionRowVersion, status: runtimeStatus, write_available: runtimeStatus === 'ACTIVE' });
    if (path === `/api/v1/flow-runs/${run.id}/runtime/pause` && request.method() === 'POST') {
      requests.push(path);
      runtimeStatus = 'STOPPED';
      sessionRowVersion += 1;
      return respond({ active_generation: 1, session_row_version: sessionRowVersion, status: runtimeStatus, write_available: false }, 202);
    }
    if (path === `/api/v1/flow-runs/${run.id}/runtime/resume` && request.method() === 'POST') {
      requests.push(path);
      runtimeStatus = 'ACTIVE';
      sessionRowVersion += 1;
      return respond({ active_generation: 1, session_row_version: sessionRowVersion, status: runtimeStatus, write_available: true }, 202);
    }
    if (path.endsWith('/records')) return respond({ items: [], next_cursor: null });
    if (path.endsWith('/stepwise-runs') || path.endsWith('/automatic-runs/summaries')) return respond([]);
    if (path.endsWith('/terminal-environments')) return respond([]);
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText: definition.name }).click();
  await page.locator('.flow-run-tree-run').filter({ hasText: run.name }).click();
  await expect(page.getByRole('button', { name: '暂停容器', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '暂停容器', exact: true }).click();
  const pauseDialog = page.getByRole('alertdialog', { name: `暂停容器“${run.name}”？` });
  await expect(pauseDialog).toContainText('停止当前运行容器以回收计算资源');
  await pauseDialog.getByRole('button', { name: '暂停', exact: true }).click();
  await expect.poll(() => requests).toContain(`/api/v1/flow-runs/${run.id}/runtime/pause`);
  await expect(page.getByRole('button', { name: '恢复容器', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '恢复容器', exact: true }).click();
  const resumeDialog = page.getByRole('alertdialog', { name: `恢复容器“${run.name}”？` });
  await expect(resumeDialog).toContainText('启动保留的运行容器');
  await resumeDialog.getByRole('button', { name: '恢复', exact: true }).click();
  await expect.poll(() => requests).toContain(`/api/v1/flow-runs/${run.id}/runtime/resume`);
  await expect(page.getByRole('button', { name: '暂停容器', exact: true })).toBeVisible();
});

test('flow library saves an empty draft before opening its node editor', async ({ page }) => {
  let saved: Record<string, unknown> | undefined;
  let flows: unknown[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flows' && request.method() === 'POST') {
      saved = request.postDataJSON() as Record<string, unknown>;
      const flow = { ...definition, ...saved, id: 'empty-draft', row_version: 1 };
      flows = [flow];
      return respond(flow, 201);
    }
    if (path === '/api/v1/flows') return respond(flows);
    if (path === '/api/v1/node-assets') return respond([asset]);
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await expect(page.locator('.flow-overview-empty')).toBeVisible();
  await page.locator('.flow-library-list-tools').getByRole('button', { name: '新建', exact: true }).click();
  await page.getByLabel('新流程名称').fill('可恢复编排草稿');
  await page.getByLabel('新流程说明').fill('先保存基本信息');
  await page.getByRole('button', { name: '保存并开始编排' }).click();
  await expect.poll(() => saved).toEqual(expect.objectContaining({ name: '可恢复编排草稿', nodes: [], edges: [] }));
  await expect(page.locator('.flow-editor-head')).toContainText('未找到一个启动节点');
  await expect(page.getByTestId('flow-library')).toContainText('节点资产目录');
  await page.getByRole('button', { name: '返回全部流程', exact: true }).click();
  await expect(page.locator('.flow-library-row')).toContainText('可恢复编排草稿');
  await expect(page.locator('.flow-library-row')).toContainText('未找到一个启动节点');
  await page.getByRole('button', { name: '操作流程 可恢复编排草稿', exact: true }).click();
  await page.locator('.flow-row-menu').getByRole('button', { name: '进入编排', exact: true }).click();
  await expect(page.getByTestId('flow-designer')).toBeVisible();
  await expect(page.getByRole('button', { name: '保存编排', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '流程名称', exact: true })).toHaveCount(0);
});


test('flow library scrolls all rows with sidebar actions and a full width editor', async ({ page }) => {
  let flows = Array.from({ length: 11 }, (_, i) => ({ ...definition, id: `flow-${i}`, name: `验收流程 ${i}` }));
  let copied: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flows' && request.method() === 'POST') {
      copied = request.postDataJSON() as Record<string, unknown>;
      return respond({ ...definition, ...copied, id: 'copied-flow' }, 201);
    }
    if (path === '/api/v1/flows') return respond(flows);
    if (path.startsWith('/api/v1/flows/') && request.method() === 'DELETE') {
      flows = flows.filter(flow => flow.id !== path.split('/').at(-1));
      return route.fulfill({ status: 204 });
    }
    if (path === '/api/v1/node-assets') return respond([asset]);
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await expect(page.locator('.flow-library-row')).toHaveCount(11);
  await expect(page.locator('.flow-library-list-panel .flow-library-list-tools')).toBeVisible();
  await expect(page.locator('.flow-library-pagination')).toHaveCount(0);
  const preview = page.getByRole('region', { name: '验收流程 0 的流程画布' });
  await expect(preview.locator('.react-flow__node')).toHaveCount(2);
  await expect(preview.locator('.react-flow__edge')).toHaveCount(2);
  await expect(preview.getByRole('button', { name: '进入编排', exact: true })).toBeVisible();
  const previewNode = preview.locator('.react-flow__node').first();
  const scale = () => preview.locator('.react-flow__viewport').evaluate(element => new DOMMatrix(getComputedStyle(element).transform).a);
  const initialScale = await scale();
  await preview.getByRole('button', { name: 'Zoom In', exact: true }).click();
  await expect.poll(scale).toBeGreaterThan(initialScale + 0.05);
  const enlargedScale = await scale();
  await preview.getByRole('button', { name: 'Zoom Out', exact: true }).click();
  await expect.poll(scale).toBeLessThan(enlargedScale - 0.05);
  await preview.getByRole('button', { name: 'Fit View', exact: true }).click();
  await expect.poll(() => preview.locator('.react-flow__node').evaluateAll(nodes => nodes.every(node => {
    const bounds = node.getBoundingClientRect();
    const canvas = node.closest('.react-flow')!.getBoundingClientRect();
    return bounds.left >= canvas.left && bounds.right <= canvas.right && bounds.top >= canvas.top && bounds.bottom <= canvas.bottom;
  }))).toBe(true);
  const beforeDrag = await previewNode.boundingBox();
  expect(beforeDrag).not.toBeNull();
  await page.mouse.move(beforeDrag!.x + beforeDrag!.width / 2, beforeDrag!.y + 20);
  await page.mouse.down();
  await page.mouse.move(beforeDrag!.x + beforeDrag!.width / 2 + 55, beforeDrag!.y + 55, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => (await previewNode.boundingBox())!.x).toBeGreaterThan(beforeDrag!.x + 30);
  const draggedTransform = await preview.locator('.react-flow__viewport').getAttribute('style');
  await preview.getByRole('button', { name: 'Fit View', exact: true }).click();
  await expect.poll(() => preview.locator('.react-flow__viewport').getAttribute('style')).not.toBe(draggedTransform);
  await page.screenshot({ path: '../../.tmp/ops36/preview-controls.png' });
  for (const [width, railWidth] of [[1440, 320], [1024, 280]]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.getByLabel('流程列表', { exact: true }).evaluate(element => element.getBoundingClientRect().width)).toBe(railWidth);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    const controls = await page.locator('.flow-library-list-tools').locator('label, button').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
    expect(controls).toHaveLength(3);
    expect(new Set(controls.map(box => Math.round(box.y + box.height / 2))).size).toBe(1);
    expect(controls[0].x + controls[0].width).toBeLessThan(controls[1].x);
    await page.screenshot({ path: `../../.tmp/ui-697/flow-toolbar-${width}.png` });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByLabel('搜索流程', { exact: true }).fill('验收流程 10');
  await expect(page.locator('.flow-library-row')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '全选', exact: true })).toHaveCount(0);
  await page.getByLabel('选择流程 验收流程 10', { exact: true }).check();
  await page.getByRole('button', { name: '删除 (1)', exact: true }).click();
  await page.getByRole('button', { name: '永久删除', exact: true }).click();
  await page.getByLabel('搜索流程', { exact: true }).fill('');
  await expect(page.locator('.flow-library-row')).toHaveCount(10);
  await page.getByRole('button', { name: '操作流程 验收流程 0', exact: true }).click();
  await page.getByRole('button', { name: '复制流程', exact: true }).click();
  await expect(page.getByTestId('flow-designer')).toBeVisible();
  await expect.poll(() => page.locator('.flow-editor-page').evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThan(800);
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await page.locator('.flow-product-layout').boundingBox();
    expect(layout!.x).toBe(0);
    expect(layout!.width).toBe(width);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `../../.tmp/ui-696/editor-${width}.png` });
  }
  expect(copied).toEqual({
    name: '验收流程 0 · 副本', description: '', default_entry_key: null,
    nodes: definition.nodes.map(node => ({
      instance_key: node.instance_key, node_asset_id: node.node_asset_id, alias: node.alias,
      position_x: node.position_x, position_y: node.position_y, config_override: {}, gates: [],
    })),
    edges: [{ source_instance_key: 'first', target_instance_key: 'second', position: 0 }],
    port_mappings: [{ source_instance_key: 'first', source_output_key: 'output_1', target_instance_key: 'second', target_input_key: 'input_1' }],
  });
});

test('flow library row clicks toggle checkboxes while preserving preview and actions', async ({ page }) => {
  const flows = [0, 1].map(index => ({ ...definition, id: `select-flow-${index}`, name: `选择验收流程 ${index}` }));
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flows') return respond(flows);
    if (path === '/api/v1/node-assets') return respond([asset]);
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  const checkbox = page.getByLabel('选择流程 选择验收流程 1', { exact: true });
  const row = page.locator('.flow-library-row').filter({ has: checkbox });
  await row.getByRole('button', { name: '选择验收流程 1', exact: true }).click();
  await expect(checkbox).toBeChecked();
  await expect(page.getByRole('region', { name: '选择验收流程 1 的流程画布' })).toBeVisible();
  await expect(page.getByRole('button', { name: '删除 (1)', exact: true })).toBeEnabled();
  await row.locator('.flow-readiness').click();
  await expect(checkbox).not.toBeChecked();
  await row.click({ position: { x: 2, y: 2 } });
  await expect(checkbox).toBeChecked();
  await row.getByRole('button', { name: '操作流程 选择验收流程 1', exact: true }).click();
  await expect(page.locator('.flow-row-menu')).toBeVisible();
  await expect(checkbox).toBeChecked();
  await page.keyboard.press('Escape');
  await checkbox.click();
  await expect(checkbox).not.toBeChecked();
  await checkbox.focus();
  await page.keyboard.press('Space');
  await expect(checkbox).toBeChecked();
  await page.keyboard.press('Space');
  await expect(checkbox).not.toBeChecked();
  await row.getByRole('button', { name: '选择验收流程 1', exact: true }).dblclick();
  await expect(page.getByTestId('flow-designer')).toBeVisible();
  await page.getByRole('button', { name: '返回全部流程', exact: true }).click();
  await expect(checkbox).toBeChecked();
  await checkbox.uncheck();
  await row.getByRole('button', { name: '操作流程 选择验收流程 1', exact: true }).click();
  await page.locator('.flow-row-menu').getByRole('button', { name: '进入编排', exact: true }).click();
  await expect(page.getByTestId('flow-designer')).toBeVisible();
  await page.getByRole('button', { name: '返回全部流程', exact: true }).click();
  await expect(checkbox).not.toBeChecked();
  await expect(page.getByRole('button', { name: '删除 (0)', exact: true })).toBeDisabled();
});

for (const kind of ['MANUAL', 'AUTOMATIC'] as const) {
  test(`outer directory creates a ${kind} record with the existing node and Git fields`, async ({ page }) => {
    let submitted: Record<string, unknown> | undefined;
    const manual = { ...run, id: 'created-manual', name: '新增验收记录', state: 'DRAFT', node_runs: [], start_node_key: 'first', stepwise_node_drafts: {}, parent_flow_run_id: run.id };
    const automatic = { ...automaticBase, name: '新增验收记录' };
    await page.route('**/api/v1/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname.replace(/^\/flowweave/, '');
      const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      if (path.endsWith('/auth/me')) return respond(authenticatedUser);
      if (path.endsWith('/auth/profile')) return respond({ display_name: '验收用户', email: 'review@example.test' });
      if (path === '/api/v1/flow-runs') return respond([run]);
      if (path === '/api/v1/flows') return respond([definition]);
      if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
      if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
      if (path.endsWith('/records')) return respond(directoryFixture(submitted && kind === 'MANUAL' ? [manual] : [], [], request.url()));
      if (path.endsWith('/stepwise-runs') && request.method() === 'POST') { submitted = request.postDataJSON(); return respond(manual); }
      if (path.endsWith('/automatic-runs') && request.method() === 'POST') { submitted = request.postDataJSON(); return respond(automatic); }
      if (path.endsWith('/stepwise-runs')) return respond(submitted && kind === 'MANUAL' ? [manual] : []);
      if (path.endsWith('/stepwise-runs/created-manual')) return respond(manual);
      if (path.endsWith('/automatic-runs/automatic-1')) return respond(automatic);
      if (path.endsWith('/automatic-runs/summaries')) return respond([]);
      if (path.endsWith('/runtime/resource')) return respond({ resource: null });
      if (path.endsWith('/runtime')) return respond({ status: 'ACTIVE', active_generation: 1, write_available: true });
      return respond([]);
    });
    await page.goto('/');
    await page.getByRole('button', { name: '流程运行', exact: true }).click();
    await page.locator('.flow-run-tree-select').click();
    await page.locator('.flow-run-tree-run').click();
    await expect(page.getByRole('button', { name: '管理运行记录', exact: true })).toHaveCount(0);
    await page.locator('.run-record-management-toolbar').getByRole('button', { name: '新增', exact: true }).click();
    const form = page.getByRole('dialog', { name: '新建运行记录', exact: true });
    await form.getByRole('button', { name: '运行类型' }).click();
    await form.getByRole('option', { name: kind === 'MANUAL' ? '逐步运行' : '连续运行' }).click();
    await expect(form.getByRole('button', { name: '导入 / 粘贴' })).toBeVisible();
    await form.getByLabel('运行记录名称', { exact: true }).fill('新增验收记录');
    await expect(form.getByRole('button', { name: '继续创建' })).toHaveCount(0);
    await expect(form.getByLabel('Git 用户', { exact: true })).toHaveValue('验收用户');
    await form.getByLabel('Git 邮箱', { exact: true }).fill('batch@example.test');
    await form.getByRole('button', { name: kind === 'MANUAL' ? '创建记录' : '创建草稿', exact: true }).click();
    await expect.poll(() => submitted).toMatchObject({ name: '新增验收记录', start_node_key: 'first', git_identity: { author: '验收用户', email: 'batch@example.test' } });
    if (kind === 'AUTOMATIC') expect(submitted).toMatchObject({ environment_version_id: run.environment_version_id, node_plans: {} });
    await expect(page.locator('.flow-run-workbench-header h1')).toHaveText('新增验收记录');
    await expect(page.locator('.flow-run-inner-rail')).toHaveCount(0);
    await expect(page.getByRole('button', { name: kind === 'MANUAL' ? '启动逐步节点' : '启动连续运行 新增验收记录' })).toHaveCount(0);
    await expect(page.locator('.run-record-detail-panel')).toHaveCSS('grid-column-start', '1');
    await expect(page.getByTestId('node-configuration-panel')).toBeVisible();
  });
}

test('create FlowRun uses a keyboard menu with only published digest-locked environments', async ({ page }) => {
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path === '/api/v1/flow-runs') return respond(submitted ? [run] : []);
    if (path.endsWith('/terminal-environments')) return respond([{ id: 'env', name: '验收环境', versions: [
      { id: 'building', state: 'BUILDING', version_no: 1, image_digest: 'sha256:a' },
      { id: 'missing-digest', state: 'READY', version_no: 2, image_digest: null },
      { id: run.environment_version_id, state: 'READY', version_no: 3, image_digest: 'sha256:b' },
    ] }]);
    if (path.endsWith(`/flows/${definition.id}/runs`) && route.request().method() === 'POST') { submitted = route.request().postDataJSON(); return respond(run); }
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').click();
  await page.locator('.flow-run-directory>header').getByRole('button', { name: '新建', exact: true }).click();
  await expect(page.locator('.start-run-modal select')).toHaveCount(0);
  const menu = page.getByRole('button', { name: '本次运行环境版本', exact: true });
  await menu.focus();
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('listbox', { name: '本次运行环境版本' }).getByRole('option')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(menu).toBeFocused();
  await expect(page.locator('.start-run-modal')).toBeVisible();
  await menu.click();
  await page.getByRole('option', { name: '验收环境 · v3', exact: true }).click();
  await page.getByRole('button', { name: '启动流程', exact: true }).click();
  await expect.poll(() => submitted).toEqual({ environment_version_id: run.environment_version_id });
  await expect(page.locator('.flow-run-tree-run.active')).toContainText(run.name);
});

for (const kind of ['MANUAL', 'AUTOMATIC'] as const) {
  test(`left sidebar starts a ready ${kind} record through the existing API`, async ({ page }) => {
    let started: Record<string, unknown> | undefined;
    const pendingAttempt = { ...attempt, state: 'WAITING_START_CONFIRMATION' };
    const manual = { ...run, id: 'manual-ready', name: '可启动逐步记录', parent_flow_run_id: run.id, start_node_key: 'first', node_runs: [{ ...nodeRun, attempts: [pendingAttempt] }] };
    const automatic = { ...automaticBase, automation_plan: { ...automaticBase.automation_plan, readiness: { ready: true, issues: [] } } };
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname.replace(/^\/flowweave/, '');
      const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      if (path.endsWith('/auth/me')) return respond(authenticatedUser);
      if (path === '/api/v1/flow-runs') return respond([run]);
      if (path === '/api/v1/flows') return respond([definition]);
      if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
      if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
      if (path.endsWith('/records')) return respond(directoryFixture([manual], [automatic], route.request().url()));
    if (path.endsWith('/stepwise-runs')) return respond([manual]);
      if (path.endsWith('/stepwise-runs/manual-ready')) return respond(manual);
      if (path.endsWith('/automatic-runs/summaries')) return respond([{ ...automatic, plan: { ...automatic.automation_plan, reachable_node_count: 2 } }]);
      if (path.endsWith('/automatic-runs/automatic-1')) return respond(automatic);
      if (path.endsWith('/confirm-start') || path.endsWith('/automatic-runs/automatic-1/start')) {
        started = route.request().postDataJSON();
        return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'START_REJECTED', message: '验收：启动暂不可用' } }) });
      }
      if (path.endsWith('/runtime/resource')) return respond({ resource: null });
      if (path.endsWith('/runtime')) return respond({ status: 'ACTIVE', active_generation: 1, write_available: true });
      return respond([]);
    });
    await page.goto('/');
    await page.getByRole('button', { name: '流程运行', exact: true }).click();
    await page.locator('.flow-run-tree-select').click();
    await page.locator('.flow-run-tree-run').click();
    const recordName = kind === 'MANUAL' ? manual.name : automatic.name;
    await page.getByRole('checkbox', { name: `选择运行记录 ${recordName}`, exact: true }).check();
    await page.getByRole('button', { name: `进入运行记录 ${recordName}` }).click();
    const start = page.getByRole('button', { name: kind === 'MANUAL' ? '启动逐步节点' : '启动连续运行 自动记录 1' });
    await expect(start).toBeEnabled();
    page.once('dialog', dialog => dialog.accept());
    await start.click();
    await expect.poll(() => started).toMatchObject(kind === 'MANUAL'
      ? { expected_state_version: attempt.state_version, startup_mode: 'PROMPT', prompt: attempt.startup_prompt }
      : { expected_row_version: automatic.row_version });
    await expect(start).toBeEnabled();
    await expect(page.locator('.flow-run-workbench-header h1')).toHaveText(kind === 'MANUAL' ? manual.name : automatic.name);
  });
}

for (const kind of ['MANUAL', 'AUTOMATIC'] as const) {
  test(`outer ${kind} creation preserves configuration import and opens the imported record`, async ({ page }) => {
    let submitted: unknown;
    const imported = kind === 'MANUAL'
      ? { ...run, id: 'imported-manual', name: '导入验收', parent_flow_run_id: run.id, start_node_key: 'first' }
      : { ...automaticBase, name: '导入验收' };
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      if (path.endsWith('/auth/me')) return respond(authenticatedUser);
      if (path.endsWith('/auth/profile')) return respond({ display_name: '验收用户', email: 'review@example.test' });
      if (path === '/api/v1/flow-runs') return respond([run]);
      if (path === '/api/v1/flows') return respond([definition]);
      if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
      if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
      if (path.endsWith('/records')) return respond(directoryFixture([], [], route.request().url()));
      if (path.endsWith('/config-imports')) { submitted = route.request().postDataJSON(); return respond([imported]); }
      if (path.endsWith('/stepwise-runs')) return respond(kind === 'MANUAL' && submitted ? [imported] : []);
      if (path.endsWith('/stepwise-runs/imported-manual') || path.endsWith('/automatic-runs/automatic-1')) return respond(imported);
      if (path.endsWith('/runtime/resource')) return respond({ resource: null });
      if (path.endsWith('/runtime')) return respond({ status: 'ACTIVE', active_generation: 1, write_available: true });
      return respond([]);
    });
    await page.goto('/');
    await page.getByRole('button', { name: '流程运行', exact: true }).click();
    await page.locator('.flow-run-tree-select').click();
    await page.locator('.flow-run-tree-run').click();
    await page.locator('.run-record-management-toolbar').getByRole('button', { name: '新增', exact: true }).click();
    const form = page.getByRole('dialog', { name: '新建运行记录', exact: true });
    await form.getByRole('button', { name: '运行类型' }).click();
    await form.getByRole('option', { name: kind === 'MANUAL' ? '逐步运行' : '连续运行' }).click();
    await form.getByRole('button', { name: '导入 / 粘贴', exact: true }).click();
    const importDialog = page.getByRole('dialog', { name: kind === 'MANUAL' ? '导入逐步运行配置' : '导入连续运行配置' });
    const paste = importDialog.getByRole('textbox');
    await paste.fill('{"format":"wrong"}');
    await importDialog.getByRole('button', { name: '确认导入', exact: true }).click();
    await expect(importDialog).toContainText('这不是');
    expect(submitted).toBeUndefined();
    const config = { format: kind === 'MANUAL' ? 'flowweave.stepwise-record-config' : 'flowweave.continuous-record-config', version: 1, records: [{ name: '导入验收', start_node_key: 'first', ...(kind === 'AUTOMATIC' ? { node_plans: {} } : { initial_configuration: { startup_prompt: '验收提示词', agent_preset: {}, gates: [], input_urls: {} } }) }] };
    await paste.fill(JSON.stringify(config));
    await importDialog.getByRole('button', { name: '确认导入', exact: true }).click();
    await expect.poll(() => submitted).toEqual(config);
    await expect(page.locator('.flow-run-workbench-header h1')).toHaveText('导入验收');
  });
}

test('record header stays on one line and filters stay fixed while only record rows scroll', async ({ page }) => {
  const currentRun = { ...run, name: 'FW-E2E-20261017 真实业务闭环重测 · 标题较长时保持单行' };
  const records = Array.from({ length: 40 }, (_, index) => ({ ...run, id: `compact-record-${index}`, name: `逐步业务记录 ${index + 1}`, parent_flow_run_id: run.id, start_node_key: 'first' }));
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([currentRun]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path.endsWith('/records')) return respond(directoryFixture(records, [], route.request().url()));
    if (path.endsWith('/stepwise-runs')) return respond(records);
    if (path.endsWith('/automatic-runs/summaries')) return respond([]);
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').click();
  await page.locator('.flow-run-tree-run').click();
  const directory = page.locator('.flow-run-record-directory');
  const header = directory.locator('.run-record-directory-header');
  const table = directory.getByRole('region', { name: '运行记录表格' });
  const toolbar = table.locator('.run-record-management-toolbar');
  const rows = table.locator('.flow-run-record-directory-list');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  await expect(toolbar.getByRole('navigation', { name: '运行记录类型' })).toBeVisible();
  await directory.getByLabel('运行信息', { exact: true }).click();
  await expect(directory.locator('.run-record-header-info dl')).toContainText('创建时间');
  await directory.locator('.run-record-header-info dl').click();
  await expect(directory.locator('.run-record-header-info dl')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(directory.locator('.run-record-header-info dl')).toBeHidden();
  await directory.getByLabel('运行信息', { exact: true }).click();
  await header.locator('h3').click();
  await expect(directory.locator('.run-record-header-info dl')).toBeHidden();
  for (const width of [1920, 1440, 1024]) {
    await page.setViewportSize({ width, height: 800 });
    await rows.evaluate(element => { element.scrollTop = 0; });
    const beforeHeader = (await header.boundingBox())!;
    const beforeToolbar = (await toolbar.boundingBox())!;
    expect(beforeHeader.height).toBeLessThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.scrollingElement!.scrollHeight - document.scrollingElement!.clientHeight)).toBeLessThanOrEqual(1);
    expect(await rows.evaluate(element => element.scrollHeight - element.clientHeight)).toBeGreaterThan(100);
    await rows.hover();
    await page.mouse.wheel(0, 2400);
    await expect.poll(() => rows.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    await expect(rows.locator('.flow-run-record-row')).toHaveCount(40);
    expect((await header.boundingBox())!.y).toBe(beforeHeader.y);
    expect((await toolbar.boundingBox())!.y).toBe(beforeToolbar.y);
    expect(await page.locator('.flow-run-directory-detail').evaluate(element => element.scrollTop)).toBe(0);
    await toolbar.getByRole('button', { name: '连续', exact: true }).click();
    await expect(table.getByText('该 FlowRun 尚未创建连续运行记录。')).toBeVisible();
    await toolbar.getByRole('button', { name: '逐步', exact: true }).click();
    await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
    await toolbar.getByRole('button', { name: '全部', exact: true }).click();
  }
  await page.setViewportSize({ width: 1440, height: 800 });
  await rows.evaluate(element => { element.scrollTop = 0; });
  await directory.screenshot({ path: '../../.tmp/ops22/record-table.png' });
});

test('mixed record pages load atomically and scrolling retries without losing selection or leaking old filters', async ({ page }) => {
  const manual = Array.from({ length: 35 }, (_, i) => ({ ...run, id: `manual-page-${i}`, name: `分页逐步 ${i}`, start_node_key: 'first', started_at: new Date(Date.parse(now) - i * 2000).toISOString() }));
  const automatic = Array.from({ length: 35 }, (_, i) => ({ ...automaticBase, id: `auto-page-${i}`, name: `分页连续 ${i}`, started_at: new Date(Date.parse(now) - i * 2000 - 1000).toISOString() }));
  let releaseFirst: () => void = () => undefined;
  const firstGate = new Promise<void>(resolve => { releaseFirst = resolve; });
  let releaseNext: () => void = () => undefined;
  const nextGate = new Promise<void>(resolve => { releaseNext = resolve; });
  let initial = true;
  let failNext = true;
  let holdAutomatic = false;
  let lateAutomaticReturned = false;
  let releaseAutomatic: () => void = () => undefined;
  const automaticGate = new Promise<void>(resolve => { releaseAutomatic = resolve; });
  let oldListRequests = 0;
  const requests: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path.endsWith('/stepwise-runs') || path.endsWith('/automatic-runs/summaries')) { oldListRequests += 1; return respond([]); }
    if (path.endsWith('/records')) {
      requests.push(url.search);
      if (holdAutomatic && url.searchParams.get('kind') === 'AUTOMATIC') {
        await automaticGate;
        await respond(directoryFixture(manual, automatic, route.request().url())).catch(() => undefined);
        lateAutomaticReturned = true;
        return;
      }
      if (initial) { initial = false; await firstGate; }
      if (url.searchParams.has('cursor') && failNext) { await nextGate; return respond({ error: { code: 'UNAVAILABLE', message: '分页暂不可用' } }, 503); }
      return respond(directoryFixture(manual, automatic, route.request().url()));
    }
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').click();
  await page.locator('.flow-run-tree-run').click();
  const table = page.getByRole('region', { name: '运行记录表格' });
  const rows = table.locator('.flow-run-record-directory-list');
  await expect(table.getByRole('status')).toHaveText('正在加载运行记录…');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(0);
  await expect(table.getByText('该 FlowRun 尚未创建运行记录。')).toHaveCount(0);
  releaseFirst();
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  await expect(rows.getByText('分页逐步 0', { exact: true })).toBeVisible();
  await expect(rows.getByText('分页连续 0', { exact: true })).toBeVisible();
  expect(oldListRequests).toBe(0);
  const selection = table.getByRole('checkbox', { name: '选择运行记录 分页逐步 0', exact: true });
  await selection.check();
  const firstRequestCount = requests.length;
  await rows.hover();
  await page.mouse.wheel(0, 2400);
  await expect.poll(() => requests.length).toBe(firstRequestCount + 1);
  await page.mouse.wheel(0, 200);
  await expect(table.getByRole('status')).toHaveText('正在加载更多…');
  expect(requests.length).toBe(firstRequestCount + 1);
  releaseNext();
  await expect(table.getByRole('alert')).toContainText('加载更多失败');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  failNext = false;
  await table.getByRole('alert').getByRole('button', { name: '重试', exact: true }).click();
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(60);
  await expect(selection).toBeChecked();
  await rows.hover();
  await page.mouse.wheel(0, 2400);
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(70);
  const labels = await rows.getByRole('checkbox').evaluateAll(elements => elements.map(element => element.getAttribute('aria-label')));
  expect(new Set(labels).size).toBe(70);
  const fetched = requests.length;
  await page.mouse.wheel(0, 2400);
  await expect(table.getByRole('status')).toHaveCount(0);
  expect(requests.length).toBe(fetched);
  await table.getByRole('button', { name: '逐步', exact: true }).click();
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  await expect(rows.locator('.flow-run-record-kind').getByText('连续', { exact: true })).toHaveCount(0);
  await expect(selection).toBeChecked();
  holdAutomatic = true;
  await table.getByRole('button', { name: '连续', exact: true }).click();
  await expect(table.getByRole('status')).toHaveText('正在加载运行记录…');
  await table.getByRole('button', { name: '全部', exact: true }).click();
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(70);
  releaseAutomatic();
  await expect.poll(() => lateAutomaticReturned).toBe(true);
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(70);
  await expect(rows.getByText('分页逐步 0', { exact: true })).toBeVisible();

});


test('record search queries all server pages, combines mode and clears safely across parent runs', async ({ page }) => {
  const records = Array.from({ length: 80 }, (_, index) => ({ ...run, id: `search-record-${index}`, name: `搜索业务 ${index}`, parent_flow_run_id: run.id, start_node_key: 'first' }));
  const requests: Array<{ q: string; kind: string; cursor: string | null; parent: string }> = [];
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run, { ...run, id: 'other-run', run_no: 2, name: '另一运行' }]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path.endsWith('/records')) {
      const q = url.searchParams.get('q') ?? '';
      requests.push({ q, kind: url.searchParams.get('kind') ?? 'ALL', cursor: url.searchParams.get('cursor'), parent: path.split('/')[4] });
      if (q === 'slow') await new Promise(resolve => setTimeout(resolve, 500));
      return respond(directoryFixture(records, [], url.href));
    }
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').click();
  await page.locator('.flow-run-tree-run').filter({ hasText: run.name }).click();
  const rows = page.locator('.flow-run-record-directory-list');
  const toolbar = page.locator('.run-record-management-toolbar');
  const search = page.getByRole('searchbox', { name: '搜索运行记录' });
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  await rows.getByRole('checkbox').first().check();
  await search.fill('  search-record-0  ');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(1);
  await expect(rows).toContainText('搜索业务 0');
  await expect(rows.locator('.flow-run-record-id')).toHaveText('ID search-record-0');
  expect(requests.at(-1)).toMatchObject({ q: 'search-record-0', cursor: null });
  await expect(toolbar).toContainText('已选择 0 条');
  await expect(toolbar.getByRole('button', { name: '删除', exact: true })).toBeDisabled();
  await toolbar.getByRole('button', { name: '逐步', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.kind).toBe('MANUAL');
  expect(requests.at(-1)?.q).toBe('search-record-0');
  await search.fill('slow');
  await expect.poll(() => requests.at(-1)?.q).toBe('slow');
  await search.fill('search-record-1');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(11);
  await expect(rows).not.toContainText('搜索业务 0');
  await search.fill('不存在的搜索');
  await expect(rows).toContainText('没有匹配的运行记录');
  await toolbar.getByRole('button', { name: '清除运行记录搜索', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(30);
  await search.fill('搜索业务');
  await expect.poll(() => requests.at(-1)?.q).toBe('搜索业务');
  await rows.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(rows.locator('.flow-run-record-row')).toHaveCount(60);
  expect(requests.at(-1)).toMatchObject({ q: '搜索业务', kind: 'MANUAL', cursor: '30' });
  await page.getByRole('button', { name: '流程编排', exact: true }).click();
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await expect(search).toHaveValue('搜索业务');
  await page.locator('.flow-run-tree-run').filter({ hasText: '另一运行' }).click();
  await expect(search).toHaveValue('');
  await expect.poll(() => requests.at(-1)?.parent).toBe('other-run');
  expect(requests.at(-1)?.q).toBe('');
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 768 });
    await expect(search).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(768);
    await page.screenshot({ path: `../../.tmp/ops30/record-search-${width}.png` });
  }
});
