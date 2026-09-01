import { expect, test, type Page, type Route } from '@playwright/test';

const runId = '00000000-0000-4000-8000-00000000a121';
const copiedRunId = '00000000-0000-4000-8000-00000000c121';
const flowId = '00000000-0000-4000-8000-00000000f121';
const environmentVersionId = '00000000-0000-4000-8000-00000000e121';
const artifactId = '00000000-0000-4000-8000-00000000d121';

const asset = {
  id: 'asset-fr121', name: '自动节点', description: '', icon_kind: 'LUCIDE', icon_value: 'bot', row_version: 1,
  inputs: [{ field_key: 'source', display_name: '来源', data_type: 'URL', description: '' }],
  outputs: [{ field_key: 'result', display_name: '结果', data_type: 'URL', description: '' }],
  executor: { startup_prompt: '处理当前节点', context_prompt: '仅使用冻结上下文', context_capability_ids: [] },
  context_capabilities: [], created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
};
const nodes = [
  { id: 'flow-node-first', instance_key: 'first', node_asset_id: asset.id, alias: '起始节点', position_x: 80, position_y: 140, config_override: {}, gates: [], asset },
  { id: 'flow-node-second', instance_key: 'second', node_asset_id: asset.id, alias: '下游节点', position_x: 440, position_y: 140, config_override: {}, gates: [], asset },
];
const flow = {
  id: flowId, name: 'FR121 自动编排', description: '', row_version: 1, default_entry_key: 'first', nodes,
  edges: [{ id: 'edge-first-second', source_instance_key: 'first', target_instance_key: 'second', position: 0 }],
  port_mappings: [{ id: 'mapping-first-second', source_instance_key: 'first', source_output_key: 'result', target_instance_key: 'second', target_input_key: 'source' }],
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
};
const environment = {
  id: 'environment-fr121', name: 'FR121 环境', description: '', row_version: 1, active_sessions: [],
  versions: [{ id: environmentVersionId, environment_id: 'environment-fr121', version_no: 1, state: 'READY', image_reference: 'runtime@sha256:test', image_digest: `sha256:${'1'.repeat(64)}`, base_image_reference: 'base@sha256:test', base_image_digest: `sha256:${'2'.repeat(64)}`, manifest: {}, runtime_compatible: true, run_reference_count: 0, reference_count: 0, created_at: '2026-09-01T00:00:00Z' }],
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
};

type Plan = {
  status: 'DRAFT' | 'FROZEN'; start_node_key: string; reachable_node_keys: string[];
  node_plans: Record<string, Record<string, unknown>>;
  readiness: { ready: boolean; issues: Array<{ code: string; node_key: string; message: string }> };
};

function readiness(plan: Plan): Plan['readiness'] {
  const issues: Plan['readiness']['issues'] = [];
  for (const key of plan.reachable_node_keys) {
    if (!plan.node_plans[key]) issues.push({ code: 'NODE_PLAN_REQUIRED', node_key: key, message: '请配置此节点的自动执行预设' });
  }
  const first = plan.node_plans.first as { artifact_ids?: Record<string, string>; input_urls?: Record<string, string> } | undefined;
  if (first && !first.artifact_ids?.source && !first.input_urls?.source) issues.push({ code: 'NODE_INPUT_REQUIRED', node_key: 'first', message: '请配置未映射输入：source' });
  return { ready: issues.length === 0, issues };
}

function detail(id: string, name: string, plan: Plan, rowVersion: number, artifacts: Array<Record<string, unknown>> = []) {
  const state = plan.status === 'FROZEN' ? 'ACTIVE' : 'DRAFT';
  return {
    id, flow_definition_id: flowId, flow_name: flow.name, flow_row_version: 1, run_no: id === runId ? 1 : 2, name, state,
    run_mode: 'AUTOMATIC', automation_plan: plan, row_version: rowVersion, active_snapshot_id: `snapshot-${id}`, active_snapshot_version: 1,
    environment_version_id: environmentVersionId, environment_version: environment.versions[0], completion_mode: null, current_node_key: null, current_node_name: null,
    current_attempt_state: null, has_pending_action: false, runtime_status: plan.status, runtime_write_available: false,
    runtime_message: plan.status === 'FROZEN' ? '自动运行计划已冻结，等待调度实现' : '自动运行尚未启动，可继续编辑编排',
    progress: { accepted: 0, terminal: 0, active: 0 }, started_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', finished_at: null,
    lark_folder_token: null, lark_folder_url: null,
    snapshots: [{ id: `snapshot-${id}`, version: 1, schema_version: 2, definition_hash: 'f'.repeat(64), definition: flow, environment_version_id: environmentVersionId, created_at: '2026-09-01T00:00:00Z' }],
    node_runs: [], artifacts,
  };
}

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
}

async function openRuns(page: Page) {
  await page.goto('/');
  await page.evaluate(() => localStorage.removeItem('flowweave-workbench'));
  await page.reload();
  await page.getByRole('button', { name: '流程运行' }).click();
}

test('automatic draft saves, freezes without execution, refreshes read-only and copies', async ({ page }) => {
  let plan: Plan = {
    status: 'DRAFT', start_node_key: 'first', reachable_node_keys: ['first', 'second'], node_plans: {},
    readiness: { ready: false, issues: [
      { code: 'NODE_PLAN_REQUIRED', node_key: 'first', message: '请配置此节点的自动执行预设' },
      { code: 'NODE_PLAN_REQUIRED', node_key: 'second', message: '请配置此节点的自动执行预设' },
    ] },
  };
  let rowVersion = 1;
  let artifacts: Array<Record<string, unknown>> = [];
  let copiedPlan: Plan | undefined;

  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/flows' && request.method() === 'GET') return json(route, [flow]);
    if (path === `/api/v1/flows/${flowId}` && request.method() === 'GET') return json(route, flow);
    if (path === '/api/v1/terminal-environments') return json(route, [environment]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return json(route, []);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') {
      const records = [detail(runId, 'FR121 自动编排', plan, rowVersion, artifacts)];
      if (copiedPlan) records.unshift(detail(copiedRunId, 'FR121 自动编排 · 副本 #2', copiedPlan, 1, artifacts.map(item => ({ ...item, id: `${item.id}-copy`, flow_run_id: copiedRunId }))));
      return json(route, records);
    }
    if (path === `/api/v1/flows/${flowId}/automatic-runs` && request.method() === 'POST') return json(route, detail(runId, 'FR121 自动编排', plan, rowVersion), 201);
    if (path === `/api/v1/flow-runs/${runId}` && request.method() === 'GET') return json(route, detail(runId, 'FR121 自动编排', plan, rowVersion, artifacts));
    if (path === `/api/v1/flow-runs/${copiedRunId}` && request.method() === 'GET' && copiedPlan) return json(route, detail(copiedRunId, 'FR121 自动编排 · 副本 #2', copiedPlan, 1, artifacts.map(item => ({ ...item, id: `${item.id}-copy`, flow_run_id: copiedRunId }))));
    if (path.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    if (path === `/api/v1/flow-runs/${runId}/nodes/first/input-artifacts` && request.method() === 'POST') {
      const body = request.postDataJSON() as { field_key: string; uri: string };
      const artifact = { id: artifactId, flow_run_id: runId, consumer_node_key: 'first', field_key: body.field_key, version_no: 1, artifact_type: 'URL', uri: body.uri, content_hash: 'a'.repeat(64), byte_size: body.uri.length, mime_type: 'text/uri-list', source: 'HUMAN_INPUT', metadata: { display_name: '来源' }, created_at: '2026-09-01T00:00:01Z' };
      artifacts = [artifact];
      return json(route, artifact, 201);
    }
    if (path === `/api/v1/automatic-runs/${runId}` && request.method() === 'PUT') {
      const body = request.postDataJSON() as { node_plans: Plan['node_plans'] };
      plan = { ...plan, node_plans: body.node_plans };
      plan.readiness = readiness(plan);
      rowVersion += 1;
      return json(route, detail(runId, 'FR121 自动编排', plan, rowVersion, artifacts));
    }
    if (path === `/api/v1/automatic-runs/${runId}/start` && request.method() === 'POST') {
      plan = { ...plan, status: 'FROZEN' };
      rowVersion += 1;
      return json(route, detail(runId, 'FR121 自动编排', plan, rowVersion, artifacts));
    }
    if (path === `/api/v1/automatic-runs/${runId}/copy` && request.method() === 'POST') {
      copiedPlan = { ...plan, status: 'DRAFT', node_plans: structuredClone(plan.node_plans) };
      return json(route, detail(copiedRunId, 'FR121 自动编排 · 副本 #2', copiedPlan, 1, artifacts.map(item => ({ ...item, id: `${item.id}-copy`, flow_run_id: copiedRunId }))), 201);
    }
    return json(route, { error: { code: 'NOT_FOUND', message: path } }, 404);
  });

  await openRuns(page);
  await page.getByRole('button', { name: '编排自动运行' }).click();
  const create = page.getByRole('dialog', { name: '编排自动运行' });
  await create.getByLabel('自动运行流程').selectOption(flowId);
  await create.getByLabel('自动运行名称').fill('FR121 自动编排');
  await create.getByLabel('自动运行环境版本').selectOption(environmentVersionId);
  await create.getByLabel('自动运行起始节点').selectOption('first');
  await create.getByRole('button', { name: '创建自动草稿' }).click();

  const editor = page.locator('.automatic-draft-editor');
  await expect(editor).toContainText('待补齐');
  await editor.getByRole('button', { name: '配置输入' }).click();
  const inputs = page.getByRole('dialog', { name: '填写节点输入' });
  await inputs.getByLabel('填写输入 source').fill('https://example.test/fr121-source');
  await inputs.getByRole('button', { name: '保存输入并继续' }).click();
  await editor.getByRole('button', { name: '保存编排' }).click();
  await expect(editor.getByRole('button', { name: '启动并冻结计划' })).toBeEnabled();
  await editor.getByRole('button', { name: '启动并冻结计划' }).click();
  await expect(page.getByText('已冻结自动计划', { exact: true }).first()).toBeVisible();
  await expect(editor.getByRole('button', { name: '复制为新编排' })).toBeVisible();
  await expect(page.locator('.run-selector-history')).toHaveCount(0);

  await page.reload();
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.locator('.run-selector-rail').getByRole('tab', { name: '自动运行' }).click();
  await page.locator('.run-selector-list > button').filter({ hasText: 'FR121 自动编排' }).first().click();
  await expect(page.locator('.automatic-draft-editor').getByRole('button', { name: '复制为新编排' })).toBeVisible();
  await page.locator('.automatic-draft-editor').getByRole('button', { name: '复制为新编排' }).click();
  await expect(page.getByRole('heading', { name: 'FR121 自动编排 · 副本 #2' })).toBeVisible();
  await expect(page.locator('.automatic-draft-editor')).toContainText('自动运行编排');
});
