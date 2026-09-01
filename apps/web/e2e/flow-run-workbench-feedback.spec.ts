import { expect, test } from '@playwright/test';

const now = '2026-09-01T00:00:00Z';
const asset = {
  id: 'asset-1', name: '测试节点', description: '', icon_kind: 'LUCIDE', icon_value: 'bot', row_version: 1,
  inputs: [{ field_key: 'input_1', display_name: 'input_1', data_type: 'URL', description: '' }],
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

test('run projection stays neutral until record selection and automatic save reports its result', async ({ page }) => {
  let saveRequests = 0;
  let submittedBody: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs` && request.method() === 'GET') return respond([automaticBase]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${automaticBase.id}` && request.method() === 'PUT') {
      saveRequests += 1;
      submittedBody = request.postDataJSON() as Record<string, unknown>;
      if (saveRequests > 1) return respond({ error: { code: 'ILLEGAL_STATE_TRANSITION', message: '当前自动运行记录已启动，不能继续修改。', details: {} } }, 409);
      const plans = submittedBody.node_plans as Record<string, unknown>;
      return respond({
        ...automaticBase, row_version: 2,
        automation_plan: {
          ...automaticBase.automation_plan, node_plans: plans,
          readiness: { ready: false, issues: [
            { code: 'NODE_INPUT_REQUIRED', node_key: 'first', message: '请配置未映射输入：input_1' },
            { code: 'NODE_PLAN_REQUIRED', node_key: 'second', message: '请配置此节点的自动执行预设' },
          ] },
        },
      });
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  const graph = page.locator('.run-graph');
  await expect(graph).toContainText('未选择运行记录，当前显示中性流程定义');
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);
  await expect(graph.getByText('运行 1 次', { exact: true })).toHaveCount(0);
  await expect(page.locator('.timeline button.active')).toHaveCount(0);

  const manualRecord = page.locator('.timeline button').filter({ hasText: '测试节点' });
  await manualRecord.click();
  await expect(graph.locator('.run-graph-node.current')).toContainText('当前激活 · 运行 1 次');
  await expect(page.locator('.timeline button.active')).toHaveCount(1);
  await manualRecord.click();
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.locator('.run-title > div').first().click();
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.locator('.run-rail > h2').click();
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 20 } });
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await page.getByRole('tab', { name: '自动运行' }).click();
  await expect(graph).toContainText('选择一条自动运行记录后，可逐个配置其可达节点。');
  await page.locator('.run-graph-node').filter({ hasText: '测试节点' }).first().click();
  await expect(page.locator('.run-side-panel')).toHaveCount(0);

  const automaticRecord = page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' });
  await automaticRecord.click();
  await expect(page.getByLabel('自动启动提示词 first')).toHaveValue('读取流程输入并完成节点工作。');
  await automaticRecord.click();
  await expect(page.locator('.automatic-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await automaticRecord.click();
  await expect(page.getByLabel('自动启动提示词 first')).toBeVisible();
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 20 } });
  await expect(page.locator('.automatic-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await automaticRecord.click();
  await expect(page.getByLabel('自动启动提示词 first')).toBeVisible();
  await page.getByRole('button', { name: '保存配置' }).click();

  await expect(page.getByRole('status')).toContainText('配置已保存，仍有 2 项待补齐');
  const feedbackBanner = page.locator('.automatic-save-feedback-banner');
  await expect(feedbackBanner).toBeVisible();
  const [bannerBox, panelBox] = await Promise.all([
    feedbackBanner.boundingBox(),
    page.locator('.run-side-panel').boundingBox(),
  ]);
  expect(bannerBox).not.toBeNull();
  expect(panelBox).not.toBeNull();
  expect(bannerBox!.x).toBeGreaterThanOrEqual(panelBox!.x);
  expect(bannerBox!.x + bannerBox!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width);
  expect(bannerBox!.y + bannerBox!.height).toBeLessThanOrEqual(panelBox!.y + panelBox!.height);
  expect((submittedBody?.node_plans as Record<string, unknown>).first).toEqual(expect.objectContaining({
    startup_prompt: '读取流程输入并完成节点工作。',
  }));

  await page.getByRole('button', { name: '保存配置' }).click();
  await expect(page.getByRole('alert')).toContainText('保存失败：当前自动运行记录已启动，不能继续修改。');
});
