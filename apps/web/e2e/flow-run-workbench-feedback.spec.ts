import { expect, test } from '@playwright/test';

const now = '2026-09-01T00:00:00Z';
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
        gates: [], artifact_ids: {}, input_urls: {},
      },
    },
  },
};

const chatAttempt = {
  ...attempt, id: 'chat-attempt-1', state: 'WAITING_START_CONFIRMATION', startup_mode: 'CHAT',
};
const chatNodeRun = { ...nodeRun, id: 'chat-node-run-1', attempts: [chatAttempt] };
const chatRun = {
  ...run, node_runs: [chatNodeRun], current_attempt_state: 'WAITING_START_CONFIRMATION',
};
const automaticAttempt = {
  ...attempt, id: 'automatic-attempt-1', node_run_id: 'automatic-node-run-1', state: 'END_BLOCKED',
  state_version: 4, error_code: 'AUTOMATIC_TRANSITION_INVALID',
  error_detail: '流转 Agent 选择了未授权节点',
};
const automaticNodeRun = {
  ...nodeRun, id: 'automatic-node-run-1', flow_run_id: automaticBase.id, attempts: [automaticAttempt],
};
const runningAutomatic = {
  ...frozenAutomaticBase, state: 'WAITING_HUMAN', runtime_status: 'ACTIVE', runtime_write_available: true,
  current_node_key: 'first', current_node_name: '测试节点', current_attempt_state: 'END_BLOCKED',
  progress: { accepted: 0, terminal: 0, active: 1 }, node_runs: [automaticNodeRun],
  automation_plan: {
    ...frozenAutomaticBase.automation_plan, status: 'FROZEN',
    readiness: { ready: true, issues: [] },
  },
};

test('run projection stays neutral until record selection and automatic save reports its result', async ({ page }) => {
  let saveRequests = 0;
  let submittedBody: Record<string, unknown> | undefined;
  const inputArtifacts = [
    {
      id: 'artifact-url', flow_run_id: automaticBase.id, producer_attempt_id: null, consumer_node_key: 'first',
      field_key: 'input_1', version_no: 1, artifact_type: 'URL', storage_key: null,
      uri: 'https://example.com/input', inline_content: null, content_hash: 'url-hash', byte_size: 25,
      mime_type: 'text/uri-list', source: 'HUMAN_INPUT', metadata: { display_name: 'input_1' }, created_at: now,
    },
    {
      id: 'artifact-file', flow_run_id: automaticBase.id, producer_attempt_id: null, consumer_node_key: 'first',
      field_key: 'input_2', version_no: 1, artifact_type: 'FILE', storage_key: 'inputs/example.md',
      uri: null, inline_content: null, content_hash: 'file-hash', byte_size: 12,
      mime_type: 'text/markdown', source: 'HUMAN_INPUT', metadata: { display_name: 'input_2', filename: 'example.md' }, created_at: now,
    },
  ];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs` && request.method() === 'GET') return respond([frozenAutomaticBase]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${automaticBase.id}/nodes/first/input-artifacts` && request.method() === 'POST') return respond(inputArtifacts[0], 201);
    if (path === `/api/v1/flow-runs/${automaticBase.id}/nodes/first/input-artifacts/upload` && request.method() === 'POST') return respond(inputArtifacts[1], 201);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${automaticBase.id}` && request.method() === 'PUT') {
      saveRequests += 1;
      submittedBody = request.postDataJSON() as Record<string, unknown>;
      if (saveRequests > 1) return respond({ error: { code: 'ILLEGAL_STATE_TRANSITION', message: '当前自动运行记录已启动，不能继续修改。', details: {} } }, 409);
      const plans = submittedBody.node_plans as Record<string, unknown>;
      return respond({
        ...automaticBase, row_version: 2, artifacts: inputArtifacts,
        automation_plan: {
          ...automaticBase.automation_plan, node_plans: plans,
          readiness: { ready: false, issues: [
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
  await expect(graph.locator('.flow-direction-edge .react-flow__edge-path')).toHaveCount(1);
  await expect(graph.locator('.flow-mapping-edge .react-flow__edge-path')).toHaveCount(1);
  await expect(graph.locator('.run-graph-node .data-port-handle')).toHaveCount(6);
  const draggableNode = graph.locator('.run-graph-node.current');
  const beforeDrag = await draggableNode.boundingBox();
  expect(beforeDrag).not.toBeNull();
  await page.mouse.move(beforeDrag!.x + 110, beforeDrag!.y + 45);
  await page.mouse.down();
  await page.mouse.move(beforeDrag!.x + 190, beforeDrag!.y + 95, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => (await draggableNode.boundingBox())?.x ?? 0).toBeGreaterThan(beforeDrag!.x + 50);
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
  await expect(graph).toContainText('未选择自动运行记录，当前显示中性流程定义；点击节点可新建单节点运行。');
  await page.locator('.run-graph-node').filter({ hasText: '测试节点' }).first().click();
  const neutralNodeConsole = page.locator('.run-side-panel .node-console');
  await expect(neutralNodeConsole).toBeVisible();
  await expect(neutralNodeConsole).toHaveAttribute('data-testid', 'node-configuration-panel');
  await expect(neutralNodeConsole.getByRole('button', { name: /提示词执行/ })).toBeVisible();
  await expect(neutralNodeConsole.getByRole('button', { name: /会话启动/ })).toBeEnabled();
  await expect(neutralNodeConsole.getByRole('navigation', { name: '提示词执行配置' })).toContainText('输入与上下文Agent 配置门禁配置执行记录');

  const automaticRecord = page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' });
  await automaticRecord.click();
  const automaticEditor = page.locator('.automatic-record-editor');
  await expect(automaticEditor).toBeVisible();
  await expect(automaticEditor).toHaveAttribute('data-testid', 'node-configuration-panel');
  await expect(automaticEditor.getByRole('button', { name: /提示词执行/ })).toHaveClass(/active/);
  await expect(automaticEditor.getByRole('button', { name: /会话启动/ })).toBeDisabled();
  await expect(automaticEditor.getByRole('navigation', { name: '提示词执行配置' })).toContainText('输入与上下文Agent 配置门禁配置执行记录');
  await expect(automaticEditor.getByRole('heading', { name: '输入' })).toBeVisible();
  await expect(automaticEditor.getByRole('heading', { name: '启动提示词' })).toBeVisible();
  await expect(automaticEditor).toContainText('读取流程输入并完成节点工作。');

  await automaticEditor.getByRole('button', { name: '填写节点输入' }).click();
  const inputDialog = page.getByRole('dialog', { name: '填写节点输入' });
  await inputDialog.getByRole('textbox', { name: '填写输入 input_1' }).fill('https://example.com/input');
  await inputDialog.getByLabel('上传输入文件 input_2').setInputFiles({
    name: 'example.md', mimeType: 'text/markdown', buffer: Buffer.from('hello input'),
  });
  await inputDialog.getByRole('button', { name: '保存输入并继续' }).click();
  await expect(inputDialog).toHaveCount(0);
  await expect(automaticEditor.getByRole('link', { name: 'https://example.com/input' })).toBeVisible();
  await expect(automaticEditor.getByRole('link', { name: 'example.md' })).toBeVisible();

  await automaticEditor.getByRole('button', { name: 'Agent 配置' }).click();
  await expect(automaticEditor.getByRole('heading', { name: '首会话 Agent 配置' })).toBeVisible();
  const [agentHintBox, firstAgentModuleBox] = await Promise.all([
    automaticEditor.locator('.agent-preset-editor > header small').boundingBox(),
    automaticEditor.locator('.agent-preset-module').first().boundingBox(),
  ]);
  expect(agentHintBox).not.toBeNull();
  expect(firstAgentModuleBox).not.toBeNull();
  expect(firstAgentModuleBox!.y - (agentHintBox!.y + agentHintBox!.height)).toBeLessThan(40);
  await automaticEditor.getByRole('button', { name: '门禁配置' }).click();
  await expect(automaticEditor).toContainText('门禁只应用于即将创建的这一次执行');
  const [gateHintBox, firstGateStageBox] = await Promise.all([
    automaticEditor.locator('.gate-draft-editor > .field-hint').boundingBox(),
    automaticEditor.locator('.gate-draft-stage').first().boundingBox(),
  ]);
  expect(gateHintBox).not.toBeNull();
  expect(firstGateStageBox).not.toBeNull();
  expect(firstGateStageBox!.y - (gateHintBox!.y + gateHintBox!.height)).toBeLessThan(40);
  await automaticEditor.getByRole('button', { name: '输入与上下文' }).click();
  await automaticRecord.click();
  await expect(page.locator('.automatic-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await automaticRecord.click();
  await expect(page.locator('.automatic-record-editor')).toBeVisible();
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 20 } });
  await expect(page.locator('.automatic-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await automaticRecord.click();
  await expect(page.locator('.automatic-record-editor')).toBeVisible();
  await page.getByRole('button', { name: '保存配置' }).click();

  await expect(page.getByRole('status')).toContainText('配置已保存，仍有 1 项待补齐');
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
    artifact_ids: { input_1: 'artifact-url', input_2: 'artifact-file' },
  }));
  expect(((submittedBody?.node_plans as Record<string, { agent_preset: Record<string, unknown> }>).first.agent_preset)).not.toHaveProperty('capabilities');
  await expect(automaticEditor.getByRole('link', { name: 'https://example.com/input' })).toBeVisible();
  await expect(automaticEditor.getByRole('link', { name: 'example.md' })).toBeVisible();

  await page.getByRole('button', { name: '保存配置' }).click();
  await expect(page.getByRole('alert')).toContainText('保存失败：当前自动运行记录已启动，不能继续修改。');
});

test('FR-130 running automatic records show execution facts and chat attempts submit explicit outputs', async ({ page }) => {
  let currentRun = chatRun;
  let submittedOutputs: unknown;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs` && request.method() === 'GET') return respond([runningAutomatic]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/node-attempts/${chatAttempt.id}/manual-outputs` && request.method() === 'POST') {
      submittedOutputs = request.postDataJSON();
      const acceptedAttempt = {
        ...chatAttempt, state: 'WAITING_ACCEPTANCE', state_version: 2, runtime_phase: 'MANUAL_OUTPUTS_SUBMITTED',
        artifacts: [{
          id: 'manual-output-1', flow_run_id: run.id, producer_attempt_id: chatAttempt.id, consumer_node_key: null,
          field_key: 'output_1', version_no: 1, artifact_type: 'URL', storage_key: null,
          uri: 'https://example.com/result', inline_content: null, content_hash: 'manual-output-hash', byte_size: 26,
          mime_type: 'text/uri-list', source: 'HUMAN_SESSION', metadata: {}, created_at: now,
        }],
      };
      currentRun = { ...currentRun, current_attempt_state: 'WAITING_ACCEPTANCE', node_runs: [{ ...chatNodeRun, attempts: [acceptedAttempt] }] };
      return respond(acceptedAttempt);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  await page.locator('.timeline button').filter({ hasText: '测试节点' }).click();
  const manualPanel = page.locator('.attempt-control');
  await expect(manualPanel.getByRole('heading', { name: '提交会话产出' })).toBeVisible();
  await expect(manualPanel).toContainText('会话回复不会自动成为节点输出');
  await manualPanel.getByLabel('提交输出 output_1').fill('https://example.com/result');
  await manualPanel.getByRole('button', { name: '提交候选输出并运行完成门禁' }).click();
  await expect(manualPanel.getByRole('button', { name: '完成节点并流转' })).toBeVisible();
  expect(submittedOutputs).toEqual({
    expected_state_version: 1,
    outputs: { output_1: { artifact_type: 'URL', uri: 'https://example.com/result' } },
  });

  await page.getByRole('tab', { name: '自动运行' }).click();
  await page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' }).click();
  await expect(page.locator('.automatic-record-editor')).toHaveCount(0);
  await expect(page.getByTestId('attempt-state')).toHaveText('END_BLOCKED');
  await expect(page.locator('.attempt-control')).toContainText('自动运行需要人工处理');
  await expect(page.locator('.attempt-control')).toContainText('流转 Agent 选择了未授权节点');
  await expect(page.locator('.run-graph-node.failed')).toContainText('完成条件未通过');
  await expect(page.locator('.run-graph-node.automatic-locked')).toContainText('测试节点2');
  await expect(page.locator('.attempt-control').getByRole('button', { name: '取消本轮节点执行' })).toHaveCount(0);
});

test('cancelled manual records return to the neutral graph and can be deleted', async ({ page }) => {
  let currentRun = run;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: status === 204 ? undefined : JSON.stringify(body),
    });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/node-attempts/${attempt.id}/cancel` && request.method() === 'POST') {
      const cancelledAttempt = { ...attempt, state: 'CANCELLED', state_version: 2, runtime_phase: 'CANCELLED' };
      currentRun = {
        ...currentRun, state: 'ACTIVE', completion_mode: null, finished_at: null,
        progress: { accepted: 0, terminal: 1, active: 0 },
        node_runs: [{ ...nodeRun, state: 'CANCELLED', attempts: [cancelledAttempt] }],
      };
      return respond(cancelledAttempt);
    }
    if (path === `/api/v1/flow-runs/${run.id}/nodes/${nodeRun.id}` && request.method() === 'DELETE') {
      currentRun = {
        ...currentRun, node_runs: [],
        progress: { accepted: 0, terminal: 0, active: 0 },
      };
      return respond(undefined, 204);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  const deleteButton = page.locator('.manual-record-toolbar').getByRole('button', { name: '删除' });
  await expect(deleteButton).toBeDisabled();
  await expect(page.getByRole('button', { name: '取消整个流程' })).toHaveCount(0);

  await page.locator('.timeline button').filter({ hasText: '测试节点' }).click();
  await expect(deleteButton).toBeDisabled();
  await page.locator('.attempt-control').getByRole('button', { name: '取消本轮节点执行' }).click();
  const cancelDialog = page.getByRole('alertdialog');
  await expect(cancelDialog).toContainText('其他节点执行和整个流程不会被取消');
  await cancelDialog.getByRole('button', { name: '取消本轮执行' }).click();

  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(page.getByTestId('flow-run-state')).toHaveText('运行中');
  await expect(page.locator('.run-graph')).toContainText('未选择运行记录，当前显示中性流程定义');

  await page.locator('.timeline button').filter({ hasText: '测试节点' }).click();
  await expect(deleteButton).toBeEnabled();
  await deleteButton.click();
  const deleteDialog = page.getByRole('alertdialog');
  await expect(deleteDialog).toContainText('FlowRun、共享 Runtime 和 OpenHands 状态继续保留');
  await deleteDialog.getByRole('button', { name: '删除', exact: true }).click();

  await expect(page.locator('.timeline button')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await page.locator('.run-graph-node').filter({ hasText: '测试节点2' }).click();
  await expect(page.locator('.run-side-panel .node-console')).toBeVisible();
});
