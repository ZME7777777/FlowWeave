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
    if (path.endsWith('/nodes/first/input-artifacts')) {
      const body = request.postDataJSON();
      expect(body).toMatchObject({ field_key: 'input_1', artifact_type: 'URL', uri: 'https://example.test/edited-input' });
      return respond({ ...inputArtifact, id: 'edited-url', uri: body.uri });
    }
    if (path.endsWith('/nodes/first/input-artifacts/upload')) {
      expect(request.headers()['content-type']).toContain('multipart/form-data');
      return respond({ ...inputArtifact, id: 'edited-file', field_key: 'input_2', artifact_type: 'FILE', uri: null, metadata: { filename: 'inputs.txt' } });
    }
    if (path.endsWith('/artifacts')) {
      const ids = url.searchParams.getAll('artifact_ids');
      const items = [inputArtifact, outputArtifact].filter(item => ids.includes(item.id));
      return respond({ items, page: 1, page_size: 20, total: items.length });
    }
    if (path.endsWith('/artifact-audit')) return respond({ items: [], page: 1, page_size: 20, total: 0 });
    if (path.endsWith('/automatic-runs/automatic-1') && request.method() === 'PUT') {
      const body = request.postDataJSON(); writes.push(body); record = { ...record, ...body, automation_plan: { ...(record.automation_plan as object), node_plans: body.node_plans }, row_version: 2 };
      return respond(record);
    }
    if (path === '/api/v1/flow-runs/manual-1/stepwise-node-drafts/first' && request.method() === 'PUT') {
      const body = request.postDataJSON(); writes.push(body); const draft = { ...(record.stepwise_node_drafts as Record<string, object>).first, ...body, row_version: 2, input_bindings: Object.entries(body.artifact_ids).map(([input_field_key, artifact_version_id]) => ({ input_field_key, artifact_version_id })) };
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
  await expect(page.locator('.run-detail-kind:visible')).toContainText(mode === 'AUTOMATIC' ? '连续' : '逐步');
  if (mode === 'MANUAL' && !await page.locator('.attempt-control').count()) await page.locator('.run-graph-node').filter({ has: page.getByText(asset.name, { exact: true }) }).click();
}

test('completed record keeps configuration and 36 capabilities in secondary dialogs beside the canvas', async ({ page }) => {
  await mocks(page, completedRecord);
  await openRecord(page, 'AUTOMATIC');
  const panel = page.locator('.attempt-control');
  await expect(panel.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await expect(panel.getByRole('region', { name: '执行配置', exact: true })).toContainText('test-model');
  await expect(panel.locator('.node-capability-context article')).toHaveCount(0);
  await expect(panel.getByRole('link', { name: inputArtifact.uri })).toHaveCount(0);
  await expect(panel.getByRole('region', { name: '上下文与能力', exact: true })).toContainText('36 项能力');
  await expect(panel.getByTestId('automatic-progress')).toHaveCount(0);
  await expect(page.locator('.run-workbench-toolbar')).toHaveCount(0);
  for (const [width, height] of [[1440, 900], [1440, 650], [1024, 768]]) {
    await page.setViewportSize({ width, height });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
    expect((await page.locator('.run-detail-header').boundingBox())!.height).toBeLessThanOrEqual(45);
    expect((await page.locator('.run-graph').boundingBox())!.height).toBeGreaterThan(height - 190);
    await expect.poll(() => page.locator('.run-graph .react-flow__node').evaluateAll(nodes => nodes.every(node => { const rect = node.getBoundingClientRect(); const canvas = node.closest('.run-graph-canvas')!.getBoundingClientRect(); return rect.left >= canvas.left && rect.right <= canvas.right && rect.top >= canvas.top && rect.bottom <= canvas.bottom; }))).toBe(true);
    for (const summary of await panel.locator('.record-detail-summary').all()) {
      expect((await summary.boundingBox())!.height).toBeLessThanOrEqual(42);
      await expect(summary).toBeInViewport();
    }
    expect(await panel.locator('.action-content').evaluate(element => element.scrollHeight <= element.clientHeight)).toBe(true);
    await page.screenshot({ path: `../../.tmp/ops34/record-completed-${width}-${height}.png` });
  }
  await page.getByRole('button', { name: '产物流转', exact: true }).click();
  await expect(page.locator('.flow-direction-edge .react-flow__edge-path').first()).toHaveCSS('opacity', '0.16');
  await page.getByRole('button', { name: '流程走向', exact: true }).click();
  await expect(page.locator('.flow-direction-edge .react-flow__edge-path').first()).toHaveCSS('opacity', '1');
  const configButton = panel.getByRole('button', { name: '查看执行配置', exact: true });
  await configButton.click();
  const config = page.getByRole('dialog', { name: '执行配置', exact: true });
  await expect(config.getByRole('region', { name: '本轮冻结配置' })).toContainText('test-model');
  await expect(config.locator('pre')).toHaveText(completedAttempt.startup_prompt);
  await page.keyboard.press('Tab');
  expect(await config.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.screenshot({ path: '../../.tmp/ops34/frozen-configuration.png' });
  await page.keyboard.press('Escape'); await expect(config).toHaveCount(0);
  await expect(configButton).toBeFocused();
  await panel.getByRole('button', { name: '查看节点输入', exact: true }).click();
  const inputs = page.getByRole('dialog', { name: '节点输入', exact: true });
  await expect(inputs.getByRole('link', { name: inputArtifact.uri })).toBeVisible();
  await expect(inputs.getByRole('textbox')).toHaveCount(0);
  await page.screenshot({ path: '../../.tmp/ops34/frozen-inputs.png' });
  await page.mouse.click(8, 100); await expect(inputs).toHaveCount(0);
  await panel.getByRole('button', { name: '查看上下文与能力', exact: true }).click();
  const context = page.getByRole('dialog', { name: '会话配置 只读', exact: true });
  await expect(context.locator('.frozen-capability-row').filter({ hasText: 'Skill' })).toHaveCount(36);
  await context.locator('.agent-capability-list').evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(context).toContainText('flowweave-capability-35');
  await expect(context.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);
  await expect(context.getByRole('checkbox')).toHaveCount(0);
  expect(await context.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: '../../.tmp/ops80/context-capabilities.png' });
  await context.getByRole('button', { name: '查看 团队背景', exact: true }).click();
  await expect(context.getByRole('region', { name: '查看 Context 团队背景' })).toContainText('该轮使用的不可变团队背景');
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await context.getByRole('button', { name: '返回列表', exact: true }).click();
  await expect(context.locator('.frozen-capability-row').filter({ hasText: 'Skill' })).toHaveCount(36);
  await expect(context.getByRole('button', { name: '查看 团队背景', exact: true })).toBeFocused();
  await page.keyboard.press('Escape'); await expect(context).toHaveCount(0);
  await panel.getByRole('button', { name: '门禁结果', exact: true }).click();
  await expect(panel).toContainText('门禁');
  await panel.getByRole('button', { name: '输出', exact: true }).click();
  await expect(panel.getByRole('region', { name: '节点输出' })).toContainText(outputArtifact.uri);
  await panel.getByRole('button', { name: '历史审计', exact: true }).click();
  await expect(panel).toContainText('未发现需要审阅的历史重复候选');
});

test('stepwise records add runtime ports with the API payload and dismiss the dialog naturally', async ({ page }) => {
  const manual = {
    ...run, id: 'manual-1', name: '运行端口记录', parent_flow_run_id: run.id, start_node_key: 'first',
    node_runs: [], artifacts: [], progress: { accepted: 0, terminal: 0, active: 0 },
  };
  await mocks(page, manual, 'MANUAL');
  let updated = structuredClone(manual);
  const portWrites: unknown[] = [];
  const endpoint = '/api/v1/flow-runs/manual-1/stepwise-runtime-ports';
  await page.route('**/api/v1/flow-runs/manual-1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === endpoint && request.method() === 'PUT') {
      const body = request.postDataJSON();
      portWrites.push(body);
      const port = body.node_ports.first.inputs.at(-1);
      updated = {
        ...updated,
        active_snapshot_version: 2,
        active_snapshot_id: 'snapshot-2',
        snapshots: [...updated.snapshots, {
          ...snapshot,
          id: 'snapshot-2',
          version: 2,
          definition: {
            ...definition,
            nodes: definition.nodes.map(node => node.instance_key === 'first'
              ? { ...node, runtime_port_overrides: { inputs: [{ ...port, required: false, runtime_port: true }], outputs: [] } }
              : node),
          },
        }],
      };
      return respond(updated);
    }
    if (path.endsWith('/stepwise-runs/manual-1') && request.method() === 'GET') return respond(updated);
    return route.fallback();
  });

  await openRecord(page, 'MANUAL');
  await page.getByRole('button', { name: '新增输入', exact: true }).first().click();
  const dialog = page.getByRole('dialog', { name: '新增本次运行输入', exact: true });
  await dialog.getByRole('textbox').first().fill('临时资料');
  await dialog.getByLabel('运行端口数据类型').click();
  await page.getByRole('option', { name: 'FILE', exact: true }).click();
  await dialog.getByRole('button', { name: '保存端口', exact: true }).click();
  await expect.poll(() => portWrites.length).toBe(1);
  expect(portWrites[0]).toEqual({
    expected_active_snapshot_version: 1,
    node_ports: { first: { inputs: [{ field_key: 'runtime_input_1', display_name: '临时资料', data_type: 'FILE', description: '' }], outputs: [] } },
    port_mappings: [],
  });
  await expect(dialog).toHaveCount(0);

  await page.getByRole('button', { name: '新增输入', exact: true }).first().click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: '新增输入', exact: true }).first().click();
  await page.locator('.modal-backdrop').click({ position: { x: 5, y: 5 } });
  await expect(dialog).toHaveCount(0);
});


for (const mode of ['AUTOMATIC', 'MANUAL'] as const) {
  test(`${mode} draft separates configuration tabs and preserves edits and save semantics`, async ({ page }) => {
    const plan = { ...frozenAutomaticBase.automation_plan.node_plans.first, agent_preset: configuredPreset, gates: [gate] };
    const initial = mode === 'AUTOMATIC' ? { ...frozenAutomaticBase, automation_plan: { ...frozenAutomaticBase.automation_plan, node_plans: { first: plan } } } : {
      ...run, id: 'manual-1', name: '逐步配置记录', parent_flow_run_id: run.id, start_node_key: 'first', node_runs: [], artifacts: [], progress: { accepted: 0, terminal: 0, active: 0 },
      stepwise_node_drafts: { first: { row_version: 1, startup_mode: 'PROMPT', startup_prompt: plan.startup_prompt, agent_preset: configuredPreset, gates: [gate], input_bindings: [] } },
    };
    const writes = await mocks(page, initial, mode);
    await openRecord(page, mode);
    const panel = page.getByTestId('node-configuration-panel');
    await expect(panel.getByRole('heading', { name: '节点输入', exact: true })).toBeVisible();
    const tab = (name: string) => panel.getByRole('tab', { name, exact: true });
    const activePanel = panel.locator('[role=tabpanel]:visible');
    await expect(activePanel).toHaveCount(1);
    await expect(panel.getByRole('heading', { name: 'Agent 配置', exact: true })).toBeHidden();
    await expect(panel.getByRole('heading', { name: '开始门禁', exact: true })).toBeHidden();
    await expect(panel.getByRole('heading', { name: '结束门禁', exact: true })).toBeHidden();
    await expect(activePanel).toContainText('大模型专属上下文');
    await panel.locator('.node-context-preset').getByRole('button', { name: '配置', exact: true }).click();
    const context = page.getByRole('dialog', { name: '配置大模型专属上下文', exact: true });
    await context.getByRole('textbox', { name: '大模型专属上下文' }).fill('本次运行的专属背景');
    await context.getByRole('button', { name: '完成', exact: true }).click();
    await panel.getByRole('switch', { name: '启用大模型专属上下文' }).click();
    await tab('输入与上下文').focus();
    await page.keyboard.press('ArrowRight');
    await expect(tab('Agent 配置')).toBeFocused();
    await expect(tab('Agent 配置')).toHaveAttribute('aria-selected', 'true');
    await expect(activePanel).toHaveAccessibleName('Agent 配置');
    await expect(activePanel.locator('.agent-preset-module')).toHaveCount(3);
    await expect(activePanel).not.toContainText('大模型专属上下文');
    await activePanel.getByRole('button', { name: /^大模型.*配置/ }).click();
    const model = page.getByRole('dialog', { name: '配置大模型', exact: true });
    await expect(model.getByRole('button', { name: '首会话模型供应商', exact: true })).toContainText('验收模型服务');
    await model.getByRole('button', { name: '完成', exact: true }).click();
    await activePanel.getByRole('button', { name: /^额度失败备用模型/ }).click();
    const fallback = page.getByRole('dialog', { name: '配置额度失败 fallback 模型', exact: true });
    await fallback.getByRole('button', { name: '取消', exact: true }).click();
    await activePanel.getByRole('button', { name: /^能力/ }).click();
    const capabilities = page.getByRole('dialog', { name: '会话配置', exact: true });
    await capabilities.getByRole('button', { name: '完成', exact: true }).click();
    await expect(panel.getByRole('heading', { name: '节点输入', exact: true })).toBeHidden();
    await tab('门禁配置').click();
    await expect(activePanel).toHaveAccessibleName('门禁配置');
    await expect(panel.getByRole('heading', { name: '结束门禁', exact: true })).toBeInViewport();
    await expect(panel.getByRole('heading', { name: '节点输入', exact: true })).toBeHidden();
    await tab('执行记录').click();
    await expect(activePanel).toContainText('还没有执行记录');
    await expect(panel.getByRole('heading', { name: '开始门禁', exact: true })).toBeHidden();
    await page.keyboard.press('Home');
    await expect(tab('输入与上下文')).toHaveAttribute('aria-selected', 'true');
    await expect(panel.getByRole('switch', { name: '启用大模型专属上下文' })).toBeChecked();
    await expect(panel.locator('.input-summary article')).toHaveCount(0);
    await panel.getByRole('button', { name: '填写节点输入', exact: true }).click();
    const inputs = page.getByRole('dialog', { name: '填写节点输入', exact: true });
    await inputs.getByRole('textbox', { name: '填写输入 input_1', exact: true }).fill('https://example.test/edited-input');
    await inputs.getByLabel('上传输入文件 input_2').setInputFiles({ name: 'inputs.txt', mimeType: 'text/plain', buffer: Buffer.from('node inputs') });
    await page.screenshot({ path: `../../.tmp/ops34/input-form-${mode}.png` });
    await inputs.getByRole('button', { name: '保存输入并继续', exact: true }).click();
    await expect(inputs).toHaveCount(0);
    await expect(panel.getByRole('region', { name: '节点输入', exact: true })).toContainText('2/2 项已配置');
    await panel.locator('.startup-prompt-summary').getByRole('button', { name: '编辑', exact: true }).click();
    const prompt = page.getByRole('dialog', { name: '编辑启动提示词' });
    await prompt.getByRole('textbox').fill('本次编辑后的启动任务');
    await prompt.getByRole('button', { name: '完成', exact: true }).click();
    await expect(panel.locator('.startup-prompt-summary')).toContainText('本次编辑后的启动任务');
    await page.getByRole('button', { name: '节点中心', exact: true }).click();
    await page.locator('.context-tab-select').filter({ hasText: /^Run #/ }).click();
    await expect(panel.locator('.startup-prompt-summary')).toContainText('本次编辑后的启动任务');
    await expect(panel.getByRole('region', { name: '节点输入', exact: true })).toContainText('2/2 项已配置');
    await panel.locator('.node-console-header-actions').getByRole('button', { name: '保存', exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    const body = writes[0];
    if (mode === 'AUTOMATIC') {
      expect(body.expected_row_version).toBe(1);
      expect((body.node_plans as Record<string, { startup_prompt: string }>).first.startup_prompt).toBe('本次编辑后的启动任务');
    } else expect(body.startup_prompt).toBe('本次编辑后的启动任务');
    const bindings = mode === 'AUTOMATIC' ? (body.node_plans as Record<string, { artifact_ids: object }>).first.artifact_ids : body.artifact_ids;
    expect(bindings).toEqual({ input_1: 'edited-url', input_2: 'edited-file' });
    const preset = mode === 'AUTOMATIC' ? (body.node_plans as Record<string, { agent_preset: object }>).first.agent_preset : body.agent_preset;
    expect(preset).toMatchObject({ node_context_prompt: '本次运行的专属背景', node_context_enabled: true });
    await expect(panel.getByRole('region', { name: '节点输入', exact: true })).toContainText('2/2 项已配置');
    for (const [width, height] of [[1440, 900], [1024, 768]]) {
      await page.setViewportSize({ width, height });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
      await panel.locator('.action-content').evaluate(element => { element.scrollTop = 0; });
      await expect(activePanel).toHaveAccessibleName('输入与上下文');
      await expect(panel.locator('.node-context-preset')).toBeInViewport();
      await page.screenshot({ path: `../../.tmp/ops79/record-inputs-${mode}-${width}.png` });
      await tab('Agent 配置').click();
      await expect(panel.getByRole('heading', { name: 'Agent 配置', exact: true })).toBeInViewport();
      await page.screenshot({ path: `../../.tmp/ops79/record-agent-${mode}-${width}.png` });
      await tab('输入与上下文').click();
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
  await expect(panel.getByRole('region', { name: '执行配置', exact: true })).toContainText('test-model');
  await expect(page.locator('.run-graph-node').filter({ hasText: '测试节点（执行时版本）' })).toBeVisible();
  await expect(panel.getByRole('button', { name: '取消本轮节点执行', exact: true })).toHaveCount(0);
  await panel.getByRole('button', { name: '输出', exact: true }).click();
  await expect(panel.getByRole('region', { name: '节点输出' })).toContainText(outputArtifact.uri);
});

const recordA = { ...completedRecord, id: 'aaaaaaaa-0000-0000-0000-000000000001', name: '记录 A' };
const recordB = { ...completedRecord, id: 'bbbbbbbb-0000-0000-0000-000000000002', name: '记录 B' };
const recordC = { ...completedRecord, id: recordA.id, name: '逐步记录 C', run_mode: 'MANUAL', start_node_key: 'first' };
const recordPath = (id: string, kind = 'automatic') => `/flow-runs/${run.id}/records/${kind}/${id}`;

async function mockRecordTabs(page: Page) {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await mocks(page, recordA);
  await page.route('**/api/v1/flow-runs/**', route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/records')) return respond({ items: [recordA, recordB, recordC].map(item => ({ kind: item.run_mode === 'MANUAL' ? 'MANUAL' : 'AUTOMATIC', id: item.id, name: item.name, state: item.state, started_at: now })), next_cursor: null });
    if (path.endsWith('/automatic-runs/summaries') || path.endsWith('/automatic-runs')) return respond([recordA, recordB]);
    if (path.endsWith('/stepwise-runs')) return respond([recordC]);
    if (path.endsWith(`/automatic-runs/${recordA.id}`)) return respond(recordA);
    if (path.endsWith(`/automatic-runs/${recordB.id}`)) return respond(recordB);
    if (path.endsWith(`/stepwise-runs/${recordC.id}`)) return respond(recordC);
    return route.fallback();
  });
}

async function enterDirectoryRecord(page: Page, name: string) {
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText: definition.name }).click();
  await page.locator('.flow-run-tree-run').filter({ hasText: run.name }).click();
  const row = page.locator('.flow-run-record-directory .flow-run-record-row').filter({ hasText: name });
  await row.locator('.flow-run-record-enter').click();
}

test('records of one FlowRun have independent URLs and tabs across switch refresh history and close', async ({ page }) => {
  await mockRecordTabs(page);
  await page.goto('/');
  await enterDirectoryRecord(page, recordA.name);
  await expect(page).toHaveURL(recordPath(recordA.id));
  await expect(page.locator('.context-tab.active .context-tab-select')).toHaveText('Run #1_aaaaaaaa');
  await page.getByRole('button', { name: '返回运行记录', exact: true }).click();
  // The full ID remains readable after search or returning from a record.
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 768 });
    const id = page.locator('.flow-run-record-row').filter({ hasText: recordA.name }).locator('.flow-run-record-id');
    await expect(id).toHaveText(`ID ${recordA.id}`);
    expect(await id.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.screenshot({ path: `../../.tmp/ops34/directory-full-id-${width}.png` });
  }
  await page.getByRole('button', { name: '收起固定导航' }).click();
  // Directory selection is retained when returning from a record.
  await page.locator('.flow-run-record-directory .flow-run-record-row').filter({ hasText: recordB.name }).locator('.flow-run-record-enter').click();
  await expect(page).toHaveURL(recordPath(recordB.id));
  await expect(page.locator('.context-tab')).toHaveCount(2);
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await page.screenshot({ path: '../../.tmp/ops28/independent-record-tabs.png' });
  await page.getByRole('button', { name: 'Run #1_aaaaaaaa', exact: true }).click();
  await expect(page).toHaveURL(recordPath(recordA.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await page.goBack();
  await expect(page).toHaveURL(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await page.goForward();
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await page.reload();
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await expect(page.locator('.context-tab')).toHaveCount(2);
  await page.getByRole('button', { name: 'Run #1_bbbbbbbb', exact: true }).click();
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await page.getByRole('button', { name: '关闭Run #1_bbbbbbbb', exact: true }).click();
  await expect(page).toHaveURL(recordPath(recordA.id));
  await expect(page.locator('.context-tab')).toHaveCount(1);
  await page.getByRole('button', { name: '关闭Run #1_aaaaaaaa', exact: true }).click();
  await expect(page).toHaveURL('/');
  await expect(page.locator('.context-tab')).toHaveCount(0);
});

test('mixed settings and record tabs close to their actual display neighbor', async ({ page }) => {
  await mockRecordTabs(page);
  await page.goto(recordPath(recordA.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '大模型配置', exact: true }).click();
  await enterDirectoryRecord(page, recordB.name);
  await expect(page.locator('.context-tab-select')).toHaveText(['Run #1_bbbbbbbb', '大模型配置', 'Run #1_aaaaaaaa']);
  await page.getByRole('button', { name: '关闭Run #1_bbbbbbbb', exact: true }).click();
  await expect(page.locator('.context-tab.active')).toHaveText('大模型配置');
  await expect(page).toHaveURL('/');
  await page.getByRole('button', { name: '关闭大模型配置', exact: true }).click();
  await expect(page).toHaveURL(recordPath(recordA.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
});

test('copied record links restore without history and mode is part of tab identity', async ({ page, context }) => {
  await mockRecordTabs(page);
  await page.goto(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await expect(page.locator('.context-tab.active .context-tab-select')).toHaveText('Run #1_bbbbbbbb');
  const fresh = await context.newPage();
  await mockRecordTabs(fresh);
  await fresh.goto(recordPath(recordA.id, 'stepwise'));
  await expect(fresh.locator('.run-detail-header:visible h1')).toHaveText(recordC.name);
  await expect(fresh.locator('.run-detail-kind:visible')).toHaveText('逐步运行');
  await fresh.evaluate(path => {
    window.history.pushState({}, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, recordPath(recordA.id));
  await expect(fresh.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await expect(fresh.locator('.run-detail-kind:visible')).toHaveText('连续运行');
  await expect(fresh.locator('.context-tab')).toHaveCount(2);
  await fresh.reload();
  await expect(fresh.locator('.context-tab')).toHaveCount(2);
  await expect(fresh.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await fresh.close();
});

test('legacy saved tabs migrate to their record and URL wins over stale history selection', async ({ page }) => {
  await mockRecordTabs(page);
  await page.addInitScript(({ parent, first, second, userId }) => {
    if (sessionStorage.getItem('record-tab-fixture')) return;
    sessionStorage.setItem('record-tab-fixture', '1');
    const state = { flowweaveFlowRun: { runId: parent, mode: 'AUTOMATIC', automaticRecordId: second } };
    sessionStorage.setItem(`flowweave.context-tabs.v1:${userId}`, JSON.stringify([
      { kind: 'flow-run', id: parent, label: 'Run #1', path: `/flow-runs/${parent}`, state },
    ]));
    if (window.location.pathname.endsWith(first)) window.history.replaceState(state, '', window.location.href);
  }, { parent: run.id, first: recordA.id, second: recordB.id, userId: authenticatedUser.id });
  await page.goto(recordPath(recordA.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordA.name);
  await expect(page.locator('.context-tab')).toHaveCount(2);
  await page.getByRole('button', { name: 'Run #1_bbbbbbbb', exact: true }).click();
  await expect(page).toHaveURL(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await expect(page.locator('.context-tab')).toHaveCount(2);
});

test('node session stays inside its record tab and browser Back restores that exact record', async ({ page }) => {
  await mockRecordTabs(page);
  await page.route('**/api/v1/flow-runs/**/agent-sessions**', route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/host')) return respond({ id: recordB.id, display_name: '节点工作区', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/runtime')) return respond({ state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/activity')) return respond({ running_binding_ids: [], condensing_binding_ids: [], possibly_stuck_binding_ids: [], failed_binding_ids: [] });
    if (path.endsWith('/agent-sessions')) return respond({ items: [], next_cursor: null });
    return route.fallback();
  });
  await page.goto(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await page.getByRole('button', { name: '查看节点会话', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/flow-runs/${recordB.id}/nodes/`));
  await expect(page.locator('.context-tab')).toHaveCount(1);
  await expect(page.locator('.context-tab.active .context-tab-select')).toHaveText('Run #1_bbbbbbbb');
  await page.goBack();
  await expect(page).toHaveURL(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await expect(page.locator('.context-tab')).toHaveCount(1);
  await page.getByRole('button', { name: '查看节点会话', exact: true }).click();
  await page.getByRole('button', { name: '返回来源', exact: true }).click();
  await expect(page).toHaveURL(recordPath(recordB.id));
  await expect(page.locator('.run-detail-header:visible h1')).toHaveText(recordB.name);
  await expect(page.locator('.context-tab')).toHaveCount(1);
});


test('configuration tabs restore their own scroll and keep the page fixed with many gates', async ({ page }) => {
  const plan = { ...frozenAutomaticBase.automation_plan.node_plans.first, agent_preset: configuredPreset, gates: Array.from({ length: 24 }, (_, index) => ({ ...gate, position: index, config: { prompt: `核对交付项目 ${index + 1}` } })) };
  await mocks(page, { ...frozenAutomaticBase, automation_plan: { ...frozenAutomaticBase.automation_plan, node_plans: { first: plan } } });
  await openRecord(page, 'AUTOMATIC');
  const panel = page.getByTestId('node-configuration-panel');
  const content = panel.locator('.action-content');
  await panel.getByRole('tab', { name: '门禁配置', exact: true }).click();
  await content.evaluate(element => { element.scrollTop = 350; });
  const previous = await content.evaluate(element => element.scrollTop);
  expect(previous).toBeGreaterThan(200);
  await panel.getByRole('tab', { name: 'Agent 配置', exact: true }).click();
  await expect.poll(() => content.evaluate(element => element.scrollTop)).toBe(0);
  await panel.getByRole('tab', { name: '门禁配置', exact: true }).click();
  await expect.poll(() => content.evaluate(element => element.scrollTop)).toBe(previous);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(1000);
  await page.screenshot({ path: '../../.tmp/ops79/gates-scroll.png' });
});


for (const mode of ['AUTOMATIC', 'MANUAL'] as const) {
  test(`${mode} frozen configuration uses session filters and never loads the current capability catalog`, async ({ page }) => {
    const historicalContext = { id: 'historical-context', capability_key: '历史团队约定', digest: 'd'.repeat(64), text: '本次冻结的旧版团队约定。' };
    const types = ['SKILL', 'MCP', 'PLUGIN', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK'];
    const frozen = types.map((capability_type, index) => ({
      id: `historical-${capability_type}`, capability_type, capability_key: `历史能力-${capability_type}`, digest: String(index).repeat(64),
      ...(capability_type === 'CONTEXT' ? { text: '冻结上下文原文' } : {}),
    }));
    const contextualNode = { ...definition.nodes[0], asset: { ...asset, context_capabilities: [{ id: 'not-applied-context', capability_key: '未应用的节点背景', digest: 'a'.repeat(64), text: '未应用的节点说明。' }] } };
    const contextualSnapshot = { ...snapshot, definition: { ...definition, nodes: [contextualNode, definition.nodes[1]] } };
    const frozenAttempt = { ...completedAttempt, frozen_session_contexts: [historicalContext], frozen_agent_capabilities: frozen };
    const record = { ...completedRecord, id: mode === 'MANUAL' ? 'manual-1' : automaticBase.id, run_mode: mode, start_node_key: 'first', snapshots: [contextualSnapshot], node_runs: [{ ...completedRecord.node_runs[0], attempts: [frozenAttempt] }] };
    const writes = await mocks(page, record, mode);
    const unsafeReads: string[] = [], mutations: string[] = [];
    page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path.includes('/capabilities') || path.includes('/website-credentials') || path.includes('/mcp-readiness')) unsafeReads.push(path);
      if (path.startsWith('/api/v1/') && request.method() !== 'GET') mutations.push(path);
    });
    await openRecord(page, mode);
    await page.getByRole('button', { name: '查看上下文与能力', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '会话配置 只读', exact: true });
    const list = dialog.locator('.agent-capability-list');
    await expect(list.locator('button')).toHaveCount(8);
    await expect(dialog.getByRole('button', { name: '关闭会话配置' })).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(dialog.getByRole('button', { name: '关闭会话配置' })).toBeFocused();
    await dialog.getByRole('button', { name: '只看已装配', exact: true }).click();
    await expect(list.locator('button')).toHaveCount(7);
    await dialog.getByRole('button', { name: '显示全部冻结项', exact: true }).click();
    const filters = dialog.getByRole('navigation', { name: '能力类型' });
    for (const [kind, title] of [['SKILL', 'skill'], ['MCP', 'MCP'], ['PLUGIN', 'plugin'], ['AGENT_DEFINITION', 'agent'], ['HOOK', 'hook']]) {
      await filters.getByRole('button', { name: title, exact: true }).click();
      await expect(list.locator('button')).toHaveCount(1);
      await list.getByRole('button', { name: `查看 历史能力-${kind}`, exact: true }).click();
      await expect(dialog).toContainText(`historical-${kind}`);
      await expect(dialog).toContainText('已装配 · 只读');
      await dialog.getByRole('button', { name: '返回列表', exact: true }).click();
      await expect(filters.getByRole('button', { name: title, exact: true })).toHaveAttribute('aria-pressed', 'true');
    }
    await filters.getByRole('button', { name: 'context', exact: true }).click();
    await expect(list.locator('button')).toHaveCount(3);
    await list.getByRole('button', { name: '查看 历史能力-CONTEXT', exact: true }).click();
    await expect(dialog.getByRole('region', { name: '查看 Context 历史能力-CONTEXT' })).toContainText('冻结上下文原文');
    await dialog.getByRole('button', { name: '返回列表', exact: true }).click();
    await dialog.getByRole('textbox', { name: '搜索冻结能力' }).fill('未应用');
    await expect(list.locator('button')).toHaveCount(1);
    await expect(list).toContainText('本轮未应用');
    await list.getByRole('button', { name: '查看 未应用的节点背景', exact: true }).click();
    await expect(dialog).toContainText('未应用 · 只读');
    await dialog.getByRole('button', { name: '返回列表', exact: true }).click();
    await expect(dialog.getByRole('textbox', { name: '搜索冻结能力' })).toHaveValue('未应用');
    await dialog.getByRole('textbox', { name: '搜索冻结能力' }).fill('不存在');
    await expect(list).toContainText('没有匹配的冻结配置');
    await dialog.getByRole('textbox', { name: '搜索冻结能力' }).fill('');
    for (const [width, height] of [[1024, 768], [390, 720]]) {
      await page.setViewportSize({ width, height });
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      await page.screenshot({ path: `../../.tmp/ops80/frozen-${mode}-${width}.png` });
    }
    await page.mouse.click(2, 2);
    await expect(dialog).toHaveCount(0);
    expect(writes).toHaveLength(0);
    expect(mutations).toEqual([]);
    expect(unsafeReads).toEqual([]);
  });
}


test('empty frozen node configuration stays explicit and read-only', async ({ page }) => {
  const emptyAttempt = { ...completedAttempt, frozen_session_contexts: [], frozen_agent_capabilities: [] };
  await mocks(page, { ...completedRecord, node_runs: [{ ...completedRecord.node_runs[0], attempts: [emptyAttempt] }] });
  await openRecord(page, 'AUTOMATIC');
  const opener = page.getByRole('button', { name: '查看上下文与能力', exact: true });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: '会话配置 只读', exact: true });
  await expect(dialog).toContainText('本轮未记录上下文或能力');
  await expect(dialog.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});


function manualDraftRecord(preset: object = configuredPreset) {
  const draft = { row_version: 1, startup_mode: 'PROMPT', startup_prompt: '执行当前节点任务', agent_preset: { capability_version_ids: [], node_context_enabled: false, ...preset }, gates: [], input_bindings: [] as Array<{ input_field_key: string; artifact_version_id: string }> };
  return { ...run, id: 'manual-1', name: '逐步配置记录', parent_flow_run_id: run.id, start_node_key: 'first', node_runs: [], artifacts: [], progress: { accepted: 0, terminal: 0, active: 0 }, stepwise_node_drafts: { first: draft, second: structuredClone(draft) } };
}

test('incomplete configuration focuses its field and missing inputs validate before writing', async ({ page }) => {
  const writes = await mocks(page, manualDraftRecord({ model_provider_id: null, model_name: null }), 'MANUAL');
  await openRecord(page, 'MANUAL');
  const panel = page.getByTestId('node-configuration-panel');
  await expect(panel.getByRole('button', { name: '启动逐步节点', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: '保存', exact: true }).click();
  await expect(panel.getByRole('tab', { name: 'Agent 配置', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(panel.locator('[data-configuration-field=model]')).toBeFocused();
  await expect(panel.locator('[data-configuration-field=model]')).toHaveAttribute('aria-invalid', 'true');
  await expect(panel.getByRole('alert')).toHaveText('请选择模型供应商和模型。');
  expect(writes).toHaveLength(0);
  await panel.getByRole('tab', { name: '输入与上下文', exact: true }).click();
  await panel.getByRole('button', { name: '填写节点输入', exact: true }).click();
  const inputs = page.getByRole('dialog', { name: '填写节点输入', exact: true });
  await inputs.getByRole('button', { name: '保存输入并继续', exact: true }).click();
  await expect(inputs.getByRole('textbox')).toBeFocused();
  await expect(inputs.getByRole('textbox')).toHaveAttribute('aria-invalid', 'true');
  await inputs.getByRole('textbox').fill('not-an-address');
  await inputs.getByRole('button', { name: '保存输入并继续', exact: true }).click();
  await expect(inputs.getByRole('alert').first()).toHaveText('请填写完整的 HTTP 或 HTTPS 地址。');
  await page.screenshot({ path: '../../.tmp/ops83/input-validation.png' });
});

test('only the execution position is current while downstream drafts stay grey and cannot start', async ({ page }) => {
  const initial = manualDraftRecord();
  const writes = await mocks(page, initial, 'MANUAL');
  await openRecord(page, 'MANUAL');
  await expect(page.locator('[data-active-flow-target=true]')).toHaveCount(1);
  await expect(page.locator('[data-active-flow-target=true]')).toContainText('测试节点');
  await page.locator('.run-graph-node').filter({ has: page.getByText('测试节点2', { exact: true }) }).click();
  const panel = page.getByTestId('node-configuration-panel');
  await expect(panel.locator('header').first()).toContainText('测试节点2');
  await expect(page.locator('.run-graph-node.snapshot-selected')).toHaveClass(/inactive/);
  await expect(page.locator('.run-graph-node.snapshot-selected')).not.toHaveAttribute('data-active-flow-target');
  await expect(panel.getByRole('button', { name: '启动逐步节点', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: '保存', exact: true })).toBeEnabled();
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 800 });
    expect((await page.locator('.run-record-detail-panel').boundingBox())!.width).toBe(350);
    const buttons = await panel.locator('.node-console-header-actions>button').evaluateAll(items => items.map(item => { const r = item.getBoundingClientRect(); return { y:r.y, height:r.height }; }));
    expect(buttons[0]).toEqual(buttons[1]);
    await expect(panel.getByRole('tab', { name: '执行记录', exact:true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.screenshot({ path: `../../.tmp/ops83/stepwise-${width}.png` });
  }
  expect(writes).toHaveLength(0);
});

test('saving a frozen projection strips audit fields and unsaved changes disable start', async ({ page }) => {
  const initial = manualDraftRecord({ ...configuredPreset, capabilities: [{ id:'frozen-audit' }] });
  initial.stepwise_node_drafts.first.input_bindings = [
    { input_field_key:'input_1', artifact_version_id:'input-url' }, { input_field_key:'input_2', artifact_version_id:'input-file' },
  ];
  const writes = await mocks(page, initial, 'MANUAL');
  await openRecord(page, 'MANUAL');
  const panel = page.getByTestId('node-configuration-panel');
  await expect(panel.getByRole('button', { name: '启动逐步节点', exact:true })).toBeEnabled();
  await panel.locator('.startup-prompt-summary').getByRole('button', { name:'编辑', exact:true }).click();
  const prompt = page.getByRole('dialog', { name:'编辑启动提示词' });
  await prompt.getByRole('textbox').fill('修改后的任务');
  await prompt.getByRole('button', { name:'完成', exact:true }).click();
  await expect(panel.getByRole('button', { name:'启动逐步节点', exact:true })).toBeDisabled();
  await panel.getByRole('button', { name:'保存', exact:true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].agent_preset).not.toHaveProperty('capabilities');
  await expect(panel.getByRole('button', { name:'启动逐步节点', exact:true })).toBeEnabled();
  let startVersion: number | undefined;
  await page.route('**/api/v1/flow-runs/manual-1/stepwise-node-drafts/first/start', route => {
    startVersion = route.request().postDataJSON().expected_row_version;
    return route.fulfill({ json:{ ...attempt, id:'started-attempt', node_run_id:'started-node', binding_id:'started-binding', state:'WAITING_START_CONFIRMATION' } });
  });
  await panel.getByRole('button', { name:'启动逐步节点', exact:true }).click();
  await expect(page).toHaveURL(/flow-runs\/manual-1\/nodes\/started-node\/attempts\/started-attempt\/agent-sessions\/started-binding$/);
  expect(startVersion).toBe(2);
});

test('server validation locates the relevant configuration instead of a generic error', async ({ page }) => {
  await mocks(page, manualDraftRecord(), 'MANUAL');
  await page.route('**/api/v1/flow-runs/manual-1/stepwise-node-drafts/first', route => route.fulfill({ status:422, json:{ error:{ code:'INVALID_COMMAND', message:'invalid', details:{ errors:[{ loc:['body','gates',0,'agent_preset','model_name'], type:'missing' }] } } } }));
  await openRecord(page, 'MANUAL');
  const panel = page.getByTestId('node-configuration-panel');
  await panel.getByRole('button', { name:'保存', exact:true }).click();
  await expect(panel.getByRole('tab', { name:'门禁配置', exact:true })).toHaveAttribute('aria-selected','true');
  await expect(panel.locator('[data-configuration-field=gates]')).toBeFocused();
  await expect(panel.getByRole('alert')).toHaveText('请检查门禁的提示词、模型和执行配置。');
  await expect(panel).not.toContainText('提交的数据不符合要求');
});

test('automatic editor and run directory keep the full presentation on return', async ({ page }) => {
  await mocks(page, frozenAutomaticBase);
  await openRecord(page, 'AUTOMATIC');
  const panel = page.getByTestId('node-configuration-panel');
  await panel.getByRole('tab', { name:'Agent 配置', exact:true }).click();
  await page.getByRole('button', { name:'节点中心', exact:true }).click();
  await page.locator('.context-tab-select').filter({ hasText:/^Run #/ }).click();
  await expect(panel.getByRole('tab', { name:'Agent 配置', exact:true })).toHaveAttribute('aria-selected','true');
  await page.getByRole('button', { name:'返回运行记录', exact:true }).click();
  await expect(page.locator('.flow-run-tree-run.active')).toContainText(run.name);
  await expect(page.locator('.flow-run-record-row')).toHaveCount(1);
});


test('returning to the directory preserves filters selection expansion details and scroll', async ({ page }) => {
  await mocks(page, frozenAutomaticBase);
  await page.route('**/api/v1/flow-runs/run-1/records?**', route => route.fulfill({ json: { items: Array.from({length:60}, (_, index) => ({ kind:'AUTOMATIC', id:index === 35 ? automaticBase.id : `other-${index}`, name:index === 35 ? automaticBase.name : `自动记录 ${index + 2}`, state:'DRAFT', started_at:now })), next_cursor:null } }));
  await page.goto('/');
  await page.getByRole('button', { name:'流程运行', exact:true }).click();
  await page.locator('.flow-run-tree-select').filter({ hasText:definition.name }).click();
  await page.locator('.flow-run-tree-run').filter({ hasText:run.name }).click();
  await page.getByRole('button', { name:'连续', exact:true }).click();
  await page.getByRole('searchbox', { name:'搜索运行记录', exact:true }).fill('自动');
  await expect(page.locator('.flow-run-record-row')).toHaveCount(60);
  const row = page.locator('.flow-run-record-row').filter({ has:page.getByText(automaticBase.name, { exact:true }) });
  await row.locator('.flow-run-record-select').click();
  await page.getByLabel('运行信息', { exact:true }).click();
  await row.locator('.flow-run-record-enter').scrollIntoViewIfNeeded();
  const scroll = await page.locator('.flow-run-record-directory-list').evaluate(element => element.scrollTop);
  expect(scroll).toBeGreaterThan(0);
  await row.locator('.flow-run-record-enter').click();
  await expect(page.getByRole('button', { name:'返回运行记录', exact:true })).toBeVisible();
  await page.getByRole('button', { name:'返回运行记录', exact:true }).click();
  await expect(page.getByRole('searchbox', { name:'搜索运行记录', exact:true })).toHaveValue('自动');
  await expect(page.locator('.run-record-filter').getByRole('button', { name:'连续', exact:true })).toHaveClass('active');
  await expect(page.locator('.flow-run-tree-select')).toHaveAttribute('aria-expanded','true');
  await expect(row).toHaveClass(/selected/);
  await expect.poll(() => page.locator('.flow-run-record-directory-list').evaluate(element => element.scrollTop)).toBe(scroll);
});


test('node capability selection reuses Agent icons filters and keeps the unsaved draft', async ({ page }) => {
  await mocks(page, manualDraftRecord(), 'MANUAL');
  const capability = { id:'skill-a', capability_key:'共享图标技能', capability_type:'SKILL', description:'测试技能', is_latest:true, revision_number:1 };
  await page.route('**/api/v1/capabilities', route => route.fulfill({ json:[capability] }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({ json:[{ id:'collection-a', name:'测试技能组', members:[capability] }] }));
  await openRecord(page, 'MANUAL');
  const panel = page.getByTestId('node-configuration-panel');
  await panel.getByRole('tab', { name:'Agent 配置', exact:true }).click();
  await panel.getByRole('button', { name:/^能力/ }).click();
  const dialog = page.getByRole('dialog', { name:'会话配置', exact:true });
  await expect(dialog.locator('.agent-capability-icon.skill>svg')).toHaveClass(/lucide-sparkles/);
  await expect(dialog.locator('.agent-selection-checkbox')).toHaveCount(1);
  await expect(dialog.getByRole('tab', { name:'认证', exact:true })).toHaveCount(0);
  await dialog.getByRole('button', { name:/^Skill 组合筛选/ }).click();
  await page.getByRole('checkbox', { name:'测试技能组', exact:true }).click();
  await page.keyboard.press('Escape');
  await dialog.getByRole('button', { name:/共享图标技能/ }).click();
  await page.keyboard.press('Escape');
  await expect(panel.getByRole('button', { name:/^能力/ })).toContainText('已选 1 项');
  await panel.getByRole('button', { name:/^能力/ }).click();
  await expect(dialog.getByRole('button', { name:/共享图标技能/ })).toHaveAttribute('aria-pressed','true');
  await page.screenshot({ path:'../../.tmp/ops83/shared-capabilities.png' });
});

test('reached successor and historical selection remain separate across temporary tabs', async ({ page }) => {
  const initial = manualDraftRecord();
  const accepted = { ...nodeRun, state:'ACCEPTED', accepted_attempt_id:completedAttempt.id, attempts:[completedAttempt] };
  await mocks(page, { ...initial, node_runs:[accepted], current_node_key:'second' }, 'MANUAL');
  await openRecord(page, 'MANUAL');
  await expect(page.locator('[data-active-flow-target=true]')).toHaveCount(1);
  await expect(page.locator('[data-active-flow-target=true]')).toContainText('测试节点2');
  await page.locator('.run-graph-node').filter({ has:page.getByText('测试节点', {exact:true}) }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await page.getByRole('button', { name:'产物流转', exact:true }).click();
  await page.getByRole('button', { name:'节点中心', exact:true }).click();
  await page.locator('.context-tab-select').filter({hasText:/^Run #/}).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await expect(page.locator('.run-graph-node.snapshot-selected')).toContainText('测试节点');
  await expect(page.getByRole('button', { name:'产物流转', exact:true })).toHaveAttribute('aria-pressed','true');
  await expect(page.locator('[data-active-flow-target=true]')).toContainText('测试节点2');
});


test('an executing stepwise node does not prevent editing an unstarted downstream draft', async ({ page }) => {
  await mocks(page, { ...manualDraftRecord(), node_runs:[nodeRun] }, 'MANUAL');
  await openRecord(page, 'MANUAL');
  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await page.locator('.run-graph-node').filter({ has:page.getByText('测试节点2', {exact:true}) }).click();
  const panel = page.getByTestId('node-configuration-panel');
  await expect(panel.locator('header').first()).toContainText('测试节点2');
  await expect(panel.getByRole('button', {name:'保存',exact:true})).toBeEnabled();
  await expect(panel.getByRole('button', {name:'启动逐步节点',exact:true})).toBeDisabled();
  await expect(page.locator('[data-active-flow-target=true]')).toHaveCount(1);
  await expect(page.locator('[data-active-flow-target=true]')).toContainText('测试节点');
});

test('a saved reached transition draft can start after its accepted predecessor', async ({ page }) => {
  const initial = manualDraftRecord();
  const noInputsSnapshot = { ...snapshot, definition:{ ...definition, nodes:definition.nodes.map(item => ({...item,asset:{...item.asset,inputs:[]}})) } };
  const accepted = { ...nodeRun, state:'ACCEPTED', accepted_attempt_id:completedAttempt.id, attempts:[completedAttempt] };
  const waiting = { ...nodeRun, id:'waiting-second', flow_node_snapshot_key:'second', sequence_no:2, created_from:'FLOW_TRANSITION', attempts:[{...attempt,id:'waiting-input',node_run_id:'waiting-second',state:'WAITING_INPUT',startup_prompt:null}] };
  await mocks(page, { ...initial, snapshots:[noInputsSnapshot], node_runs:[accepted,waiting] }, 'MANUAL');
  await openRecord(page, 'MANUAL');
  await page.locator('.run-graph-node').filter({ has:page.getByText('测试节点2', {exact:true}) }).click();
  await expect(page.getByTestId('node-configuration-panel').getByRole('button', {name:'启动逐步节点',exact:true})).toBeEnabled();
  await expect(page.locator('[data-active-flow-target=true]')).toHaveCount(1);
  await expect(page.locator('[data-active-flow-target=true]')).toContainText('测试节点2');
});
