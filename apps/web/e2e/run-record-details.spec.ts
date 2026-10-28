import { expect, test, type Page } from '@playwright/test';

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

const frozenAutomaticBase = {
  ...automaticBase,
  automation_plan: {
    ...automaticBase.automation_plan,
    node_plans: {
      first: {
        startup_prompt: asset.executor.startup_prompt,
        agent_preset: {
          capability_version_ids: [], node_context_enabled: false, node_context_prompt: '',
          model_provider_id: null, model_name: null, reasoning_effort: null, capabilities: [],
        },
        gates: [], artifact_ids: {}, input_urls: { input_1: 'https://example.com/default-input' },
      },
    },
  },
};

const inputArtifact = { id: 'input-artifact', flow_run_id: automaticBase.id, field_key: 'input_1', version_no: 1, artifact_type: 'URL', uri: 'https://example.test/frozen-input', content_hash: 'a'.repeat(64), byte_size: 120, mime_type: 'text/uri-list', source: 'HUMAN_INPUT', metadata: { display_name: '输入资料' }, created_at: now };
const outputArtifact = { ...inputArtifact, id: 'output-artifact', field_key: 'output_1', uri: 'https://example.test/final-output', source: 'RUNTIME', producer_attempt_id: 'finished-attempt' };
const configuredPreset = { model_provider_id: 'provider-1', model_name: 'test-model', reasoning_effort: 'high', capability_version_ids: [], node_context_enabled: false };
const gate = { id: 'gate-1', stage: 'END', position: 0, gate_type: 'PROMPT', enabled: true, config: { prompt: '核对本次节点交付完整。' }, agent_preset: { model_provider_id: 'provider-1', model_name: 'test-model' } };
const completedAttempt = {
  ...attempt, id: 'finished-attempt', state: 'ACCEPTED', state_version: 8,
  agent_preset: configuredPreset, startup_prompt: '根据冻结资料完成交付。'.repeat(20),
  automatic_progress: { stage: 'FINISHED', attempts: 1, max_attempts: 3, needs_attention: false },
  input_bindings: [{ input_field_key: 'input_1', artifact_version_id: inputArtifact.id, binding_source: 'AUTOMATIC_PORT_MAPPING' }],
  frozen_session_contexts: [{ id: 'context', capability_key: '团队背景', text: '该轮使用的不可变团队背景。', digest: 'b'.repeat(64) }],
  frozen_agent_capabilities: Array.from({ length: 36 }, (_, index) => ({ id: `skill-${index}`, capability_key: `flowweave-capability-${index}`, capability_type: 'SKILL', digest: 'c'.repeat(64) })),
  candidate_output_set: { id: 'outputs', completion_event_id: 'complete', artifact_ids: [outputArtifact.id], status: 'GATE_PASSED', created_at: now },
  gate_policies: [gate],
};
const completedRecord = {
  ...frozenAutomaticBase, state: 'COMPLETED', name: '异常收集与修复 · 连续运行记录', current_node_key: 'first', progress: { accepted: 1, terminal: 1, active: 0 },
  node_runs: [{ ...nodeRun, flow_run_id: automaticBase.id, state: 'ACCEPTED', accepted_attempt_id: completedAttempt.id, attempts: [completedAttempt] }],
  automation_plan: { ...frozenAutomaticBase.automation_plan, status: 'FROZEN', readiness: { ready: true, issues: [] } },
};

async function mocks(page: Page, initial: object, mode: 'AUTOMATIC' | 'MANUAL' = 'AUTOMATIC') {
  let record = structuredClone(initial) as Record<string, unknown>;
  const writes: Array<Record<string, unknown>> = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run]);
    if (path === '/api/v1/flows') return respond([definition]);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond({ ...run, node_runs: [] });
    if (path === '/api/v1/model-providers') return respond([{ id: 'provider-1', name: '验收模型服务', available_for_nodes: true, models: [{ model_name: 'test-model', enabled: true, supported_reasoning_efforts: ['high', 'medium'] }] }]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/terminal-environments') return respond([]);
    if (path.endsWith('/records')) return respond({ items: [{ kind: mode === 'AUTOMATIC' ? 'AUTOMATIC' : 'MANUAL', id: record.id, name: record.name, state: record.state, started_at: now, detail: '测试运行记录' }], next_cursor: null });
    if (path.endsWith('/stepwise-template-readiness')) return respond({ ready: true, issues: [], nodes: [], reachable_node_keys: ['first', 'second'] });
    if (path.endsWith('/automatic-runs/summaries')) return respond(mode === 'AUTOMATIC' ? [record] : []);
    if (path.endsWith('/stepwise-runs')) return respond(mode === 'MANUAL' ? [record] : []);
    if (path.endsWith('/artifacts')) {
      const ids = url.searchParams.getAll('artifact_ids');
      const items = [inputArtifact, outputArtifact].filter(item => ids.includes(item.id));
      return respond({ items, page: 1, page_size: 20, total: items.length });
    }
    if (path.endsWith('/artifact-audit')) return respond({ items: [], page: 1, page_size: 20, total: 0 });
    if (path.endsWith('/automatic-runs/automatic-1') && request.method() === 'PUT') {
      const body = request.postDataJSON(); writes.push(body); record = { ...record, ...body, row_version: 2 };
      return respond(record);
    }
    if (path === '/api/v1/flow-runs/manual-1/stepwise-node-drafts/first' && request.method() === 'PUT') {
      const body = request.postDataJSON(); writes.push(body); const draft = { ...(record.stepwise_node_drafts as Record<string, object>).first, ...body, row_version: 2, input_bindings: [] };
      record = { ...record, stepwise_node_drafts: { first: draft } }; return respond(draft);
    }
    if (path.endsWith('/automatic-runs/automatic-1') || path.endsWith('/stepwise-runs/manual-1') || path.endsWith('/flow-runs/manual-1')) return respond(record);
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: path } }, 404);
  });
  return writes;
}

async function openRecord(page: Page, mode: 'AUTOMATIC' | 'MANUAL') {
  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText: definition.name }).click();
  await page.locator('.flow-run-tree-run').filter({ hasText: run.name }).click();
  const directory = page.locator('.flow-run-record-directory');
  await expect(directory.locator('.flow-run-record-enter')).toHaveCount(1);
  await directory.locator('.flow-run-record-enter').click();
  await expect(page.getByRole('button', { name: '返回运行记录', exact: true })).toBeVisible();
  await expect(page.locator('.run-detail-kind')).toContainText(mode === 'AUTOMATIC' ? '连续' : '逐步');
  if (mode === 'MANUAL' && !await page.locator('.attempt-control').count()) await page.locator('.run-graph-node').filter({ has: page.getByText(asset.name, { exact: true }) }).click();
}

test('completed record fits a compact header, frozen configuration and 36 capabilities beside the canvas', async ({ page }) => {
  await mocks(page, completedRecord);
  await openRecord(page, 'AUTOMATIC');
  const panel = page.locator('.attempt-control');
  await expect(panel.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await expect(panel.getByRole('region', { name: '本轮冻结配置' })).toContainText('test-model');
  await expect(panel.locator('.node-capability-context article')).toHaveCount(36);
  await expect(panel.getByRole('link', { name: inputArtifact.uri })).toBeVisible();
  await expect(panel.getByTestId('automatic-progress')).toHaveCount(0);
  await expect(page.locator('.run-workbench-toolbar')).toHaveCount(0);
  for (const [width, height] of [[1440, 900], [1440, 650], [1024, 768]]) {
    await page.setViewportSize({ width, height });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
    expect((await page.locator('.run-detail-header').boundingBox())!.height).toBeLessThanOrEqual(45);
    expect((await page.locator('.run-graph').boundingBox())!.height).toBeGreaterThan(height - 190);
    await expect.poll(() => page.locator('.run-graph .react-flow__node').evaluateAll(nodes => nodes.every(node => { const rect = node.getBoundingClientRect(); const canvas = node.closest('.run-graph-canvas')!.getBoundingClientRect(); return rect.left >= canvas.left && rect.right <= canvas.right && rect.top >= canvas.top && rect.bottom <= canvas.bottom; }))).toBe(true);
    const list = await panel.locator('.node-capability-context>div').boundingBox();
    expect(list!.height).toBeLessThan(340);
    await page.screenshot({ path: `../../.tmp/ops27/record-completed-${width}-${height}.png` });
  }
  await page.getByRole('button', { name: '产物流转', exact: true }).click();
  await expect(page.locator('.flow-direction-edge .react-flow__edge-path').first()).toHaveCSS('opacity', '0.16');
  await page.getByRole('button', { name: '流程走向', exact: true }).click();
  await expect(page.locator('.flow-direction-edge .react-flow__edge-path').first()).toHaveCSS('opacity', '1');
  await panel.getByRole('button', { name: '全文', exact: true }).click();
  const prompt = page.getByRole('dialog', { name: '查看冻结启动提示词' });
  await expect(prompt.locator('pre')).toHaveText(completedAttempt.startup_prompt);
  await page.keyboard.press('Escape'); await expect(prompt).toHaveCount(0);
  await panel.getByRole('button', { name: '查看 团队背景', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '查看 Context 团队背景' })).toContainText('该轮使用的不可变团队背景');
  await page.keyboard.press('Escape');
  await panel.getByRole('button', { name: '门禁结果', exact: true }).click();
  await expect(panel).toContainText('门禁');
  await panel.getByRole('button', { name: '输出', exact: true }).click();
  await expect(panel.getByRole('region', { name: '节点输出' })).toContainText(outputArtifact.uri);
  await panel.getByRole('button', { name: '历史审计', exact: true }).click();
  await expect(panel).toContainText('未发现需要审阅的历史重复候选');
});

for (const mode of ['AUTOMATIC', 'MANUAL'] as const) {
  test(`${mode} draft shows inputs Agent and gate configuration together and preserves save semantics`, async ({ page }) => {
    const plan = { ...frozenAutomaticBase.automation_plan.node_plans.first, agent_preset: configuredPreset, gates: [gate] };
    const initial = mode === 'AUTOMATIC' ? { ...frozenAutomaticBase, automation_plan: { ...frozenAutomaticBase.automation_plan, node_plans: { first: plan } } } : {
      ...run, id: 'manual-1', name: '逐步配置记录', parent_flow_run_id: run.id, start_node_key: 'first', node_runs: [], artifacts: [], progress: { accepted: 0, terminal: 0, active: 0 },
      stepwise_node_drafts: { first: { row_version: 1, startup_mode: 'PROMPT', startup_prompt: plan.startup_prompt, agent_preset: configuredPreset, gates: [gate], input_bindings: [] } },
    };
    const writes = await mocks(page, initial, mode);
    await openRecord(page, mode);
    const panel = page.getByTestId('node-configuration-panel');
    await expect(panel.getByRole('heading', { name: '输入', exact: true })).toBeVisible();
    await expect(panel.getByRole('heading', { name: 'Agent 配置', exact: true })).toBeVisible();
    await expect(panel.getByRole('heading', { name: '开始门禁', exact: true })).toBeVisible();
    await expect(panel.getByRole('heading', { name: '结束门禁', exact: true })).toBeVisible();
    await expect(panel.locator('.node-configuration-sections>section')).toHaveCount(4);
    await panel.getByRole('button', { name: '门禁配置', exact: true }).click();
    await expect(panel.getByRole('heading', { name: '结束门禁', exact: true })).toBeInViewport();
    await expect(panel.getByRole('heading', { name: '输入', exact: true })).toBeVisible();
    await panel.getByRole('button', { name: '输入与上下文', exact: true }).click();
    await panel.locator('.startup-prompt-summary').getByRole('button', { name: '编辑', exact: true }).click();
    const prompt = page.getByRole('dialog', { name: '编辑启动提示词' });
    await prompt.getByRole('textbox').fill('本次编辑后的启动任务');
    await prompt.getByRole('button', { name: '完成', exact: true }).click();
    await expect(panel.locator('.startup-prompt-summary')).toContainText('本次编辑后的启动任务');
    await panel.locator('.node-console-header-actions').getByRole('button', { name: '保存配置', exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    const body = writes[0];
    if (mode === 'AUTOMATIC') {
      expect(body.expected_row_version).toBe(1);
      expect((body.node_plans as Record<string, { startup_prompt: string }>).first.startup_prompt).toBe('本次编辑后的启动任务');
    } else expect(body.startup_prompt).toBe('本次编辑后的启动任务');
    for (const [width, height] of [[1440, 900], [1024, 768]]) {
      await page.setViewportSize({ width, height });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
      await panel.locator('.action-content').evaluate(element => { element.scrollTop = 0; });
      await expect(panel.getByRole('heading', { name: 'Agent 配置', exact: true })).toBeInViewport();
      await page.screenshot({ path: `../../.tmp/ops27/record-draft-${mode}-${width}.png` });
    }
  });
}

test('running record keeps human input and confirmed cancellation ahead of frozen configuration', async ({ page }) => {
  const activeAttempt = { ...completedAttempt, state: 'WAITING_HUMAN', automatic_progress: { stage: 'WAITING_HUMAN', attempts: 1, max_attempts: 3, needs_attention: false } };
  const active = { ...completedRecord, state: 'WAITING_HUMAN', node_runs: [{ ...completedRecord.node_runs[0], state: 'ACTIVE', attempts: [activeAttempt] }] };
  await mocks(page, active);
  const requests: Array<{ path: string; body: unknown }> = [];
  await page.route('**/api/v1/node-attempts/finished-attempt/*', route => {
    const path = new URL(route.request().url()).pathname;
    requests.push({ path, body: route.request().postDataJSON() });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(activeAttempt) });
  });
  await openRecord(page, 'AUTOMATIC');
  const panel = page.locator('.attempt-control');
  await expect(panel.getByTestId('attempt-state')).toHaveText('WAITING_HUMAN');
  await panel.getByLabel('人工输入').fill('按既定验收要求继续。');
  await panel.getByRole('button', { name: '提交并继续', exact: true }).click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]).toEqual({ path: '/api/v1/node-attempts/finished-attempt/human-input', body: { content: '按既定验收要求继续。', expected_state_version: 8 } });
  await panel.getByRole('button', { name: '取消本轮节点执行', exact: true }).click();
  const confirm = page.getByRole('alertdialog');
  await expect(confirm).toContainText('其他节点执行和整个流程不会被取消');
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  expect(requests).toHaveLength(1);
  await panel.getByRole('button', { name: '取消本轮节点执行', exact: true }).click();
  await confirm.getByRole('button', { name: '取消本轮执行', exact: true }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toEqual({ path: '/api/v1/node-attempts/finished-attempt/cancel', body: { expected_state_version: 8 } });
});

test('completed stepwise history uses the same frozen configuration and keeps its snapshot and outputs', async ({ page }) => {
  const historical = { ...snapshot, id: 'historical-snapshot', definition_hash: 'historic', definition: { ...definition, nodes: definition.nodes.map(node => ({ ...node, alias: `${node.alias}（执行时版本）` })) } };
  const historicalAttempt = { ...completedAttempt, snapshot_id: historical.id, artifacts: [outputArtifact] };
  const manual = { ...completedRecord, id: 'manual-1', run_mode: 'MANUAL', name: '已完成逐步记录', start_node_key: 'first', snapshots: [historical, snapshot], active_snapshot_id: snapshot.id,
    artifacts: [inputArtifact, outputArtifact], node_runs: [{ ...nodeRun, flow_run_id: 'manual-1', state: 'ACCEPTED', attempts: [historicalAttempt] }] };
  await mocks(page, manual, 'MANUAL');
  await openRecord(page, 'MANUAL');
  const panel = page.locator('.attempt-control');
  await expect(panel.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await expect(panel.getByRole('region', { name: '本轮冻结配置' })).toContainText('test-model');
  await expect(page.locator('.run-graph-node').filter({ hasText: '测试节点（执行时版本）' })).toBeVisible();
  await expect(panel.getByRole('button', { name: '取消本轮节点执行', exact: true })).toHaveCount(0);
  await panel.getByRole('button', { name: '输出', exact: true }).click();
  await expect(panel.getByRole('region', { name: '节点输出' })).toContainText(outputArtifact.uri);
});
