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


test('record workspace selects mixed records and keeps runtime detail inside the page', async ({ page }) => {
  const manual = { ...run, id: 'manual-record', name: '逐步验收记录', parent_flow_run_id: run.id, start_node_key: 'first' };
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/flowweave/, '');
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path.endsWith('/stepwise-runs/manual-record')) return respond(manual);
    if (path.endsWith('/stepwise-runs')) return respond([manual]);
    if (path.endsWith('/automatic-runs/summaries')) return respond([{ ...automaticBase, plan: { ...automaticBase.automation_plan, reachable_node_count: 2 } }]);
    if (path.endsWith('/automatic-runs/automatic-1')) return respond(automaticBase);
    if (path.endsWith('/runtime/resource')) return respond({ resource: null });
    if (path.endsWith('/runtime')) return respond({ status: 'ACTIVE', active_generation: 1, write_available: false });
    return respond([]);
  });
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await expect(page.locator('.run-directory-empty')).toContainText('选择一个 FlowRun 目录');
  await page.locator('.flow-run-directory-list').getByRole('button', { name: /测试运行/ }).click();
  const directory = page.locator('.flow-run-record-directory');
  await expect(directory.locator('.flow-run-record-directory-list>button')).toHaveCount(2);
  await directory.getByRole('button', { name: '连续', exact: true }).click();
  await expect(directory.locator('.flow-run-record-directory-list>button')).toHaveCount(1);
  await directory.getByRole('button', { name: /自动记录 1/ }).click();
  await expect(page.locator('.run-record-information h2')).toHaveText(automaticBase.name);
  await expect(page.locator('.run-graph')).toBeVisible();
  const tabs = page.getByRole('navigation', { name: '运行记录信息' });
  await expect(tabs.getByRole('button', { name: '信息与配置', exact: true })).toHaveClass('active');
  await expect(page.locator('.run-record-selection-panel')).toBeVisible();
  await tabs.getByRole('button', { name: '容器', exact: true }).click();
  await expect(page.locator('.run-record-runtime-panel')).toContainText('容器与运行环境');
  await tabs.getByRole('button', { name: '终端', exact: true }).click();
  await expect(page.locator('.run-record-terminal-panel')).toContainText('运行环境当前不可写');
  await page.getByRole('button', { name: '管理运行记录', exact: true }).click();
  const manager = page.getByRole('dialog', { name: '管理运行记录', exact: true });
  await expect(manager.locator('.unified-run-record')).toHaveCount(3);
  await manager.locator('.unified-run-record').filter({ hasText: manual.name }).getByRole('button').click();
  await manager.getByRole('tab', { name: '逐步', exact: true }).click();
  await expect(manager.getByRole('button', { name: '拷贝', exact: true })).toBeEnabled();
  await expect(manager.getByRole('button', { name: '删除', exact: true })).toBeEnabled();
  await manager.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.run-record-information h2')).toHaveText(manual.name);
  await expect(tabs.getByRole('button', { name: '信息与配置', exact: true })).toHaveClass('active');
  await page.getByRole('button', { name: '返回 FlowRun 目录', exact: true }).click();
  await page.locator('.flow-run-directory-list').getByRole('button', { name: /测试运行/ }).click();
  await page.getByRole('button', { name: '管理运行记录', exact: true }).click();
  await page.getByRole('button', { name: '新建运行记录', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '选择运行类型' });
  await expect(dialog.getByRole('button', { name: /逐步运行/ })).toBeVisible();
  await expect(dialog.getByRole('button', { name: /连续运行/ })).toBeVisible();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
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
  await page.locator('.flow-overview-head').getByRole('button', { name: '新建流程' }).click();
  await page.getByLabel('新流程名称').fill('可恢复编排草稿');
  await page.getByLabel('新流程说明').fill('先保存基本信息');
  await page.getByRole('button', { name: '保存并开始编排' }).click();
  await expect.poll(() => saved).toEqual(expect.objectContaining({ name: '可恢复编排草稿', nodes: [], edges: [] }));
  await expect(page.locator('.flow-editor-head')).toContainText('未找到一个启动节点');
  await expect(page.getByTestId('flow-library')).toContainText('节点资产目录');
  await page.getByRole('button', { name: '全部流程', exact: true }).click();
  await expect(page.locator('.flow-library-row')).toContainText('可恢复编排草稿');
  await expect(page.locator('.flow-library-row')).toContainText('未找到一个启动节点');
  await page.getByRole('button', { name: '编辑流程 可恢复编排草稿', exact: true }).click();
  await expect(page.locator('.flow-editor-head')).toContainText('可恢复编排草稿');
});


test('flow library pagination retains bulk deletion and copying after the merge', async ({ page }) => {
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
  await expect(page.locator('.flow-library-row')).toHaveCount(10);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('.flow-library-row')).toHaveCount(1);
  await page.getByRole('button', { name: '全选本页', exact: true }).click();
  await page.getByRole('button', { name: '删除 (1)', exact: true }).click();
  await page.getByRole('button', { name: '永久删除', exact: true }).click();
  await expect(page.locator('.flow-library-row')).toHaveCount(10);
  await expect(page.locator('.flow-library-pagination')).toContainText('第 1 / 1 页');
  await page.getByRole('button', { name: '复制流程 验收流程 0', exact: true }).click();
  await expect(page.locator('.flow-editor-head')).toContainText('验收流程 0 · 副本');
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
