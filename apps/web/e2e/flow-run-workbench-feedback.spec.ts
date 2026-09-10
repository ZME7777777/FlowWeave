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
  error_detail: '历史自动流转状态无效',
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

test('step configuration is saved before start and direct launch has its own tab', async ({ page }) => {
  const noInputAsset = { ...asset, inputs: [] };
  const stepDefinition = {
    ...definition,
    nodes: [{ ...definition.nodes[0], asset: noInputAsset }],
    edges: [],
    port_mappings: [],
  };
  const stepSnapshot = { ...snapshot, definition: stepDefinition };
  let currentRun = {
    ...run,
    snapshots: [stepSnapshot],
    active_snapshot_id: stepSnapshot.id,
    node_runs: [],
    progress: { accepted: 0, terminal: 0, active: 0 },
  };
  let savedBody: Record<string, unknown> | undefined;
  let startBody: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
    const respond = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([stepDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(stepDefinition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}/nodes/first/runs` && request.method() === 'POST') {
      savedBody = request.postDataJSON() as Record<string, unknown>;
      const savedAttempt = {
        ...attempt,
        id: 'saved-attempt',
        node_run_id: 'saved-node-run',
        state: 'WAITING_START_CONFIRMATION',
        runtime_phase: null,
        startup_prompt: savedBody.startup_prompt,
      };
      const savedRecord = {
        ...nodeRun,
        id: 'saved-node-run',
        created_from: 'HUMAN_START',
        attempts: [savedAttempt],
      };
      currentRun = { ...currentRun, node_runs: [savedRecord], progress: { accepted: 0, terminal: 0, active: 1 } };
      return respond(savedRecord, 201);
    }
    if (path === '/api/v1/node-attempts/saved-attempt/confirm-start' && request.method() === 'POST') {
      startBody = request.postDataJSON() as Record<string, unknown>;
      const started = { ...currentRun.node_runs[0].attempts[0], state: 'EXECUTING', state_version: 2, runtime_phase: 'STARTING' };
      currentRun = { ...currentRun, node_runs: [{ ...currentRun.node_runs[0], attempts: [started] }] };
      return respond(started);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await expect(page.getByRole('tab', { name: '逐步运行' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: '连续运行' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '直接启动' })).toBeVisible();

  await page.locator('.run-graph-node').filter({ hasText: '测试节点' }).click();
  const consolePanel = page.locator('.node-console');
  await expect(consolePanel.locator('.node-console-mode-summary')).toContainText('逐步运行');
  await consolePanel.getByRole('button', { name: '保存配置' }).click();
  await expect.poll(() => savedBody).toBeTruthy();
  expect(savedBody).toEqual(expect.objectContaining({
    startup_mode: 'PROMPT',
    startup_prompt: '读取流程输入并完成节点工作。',
  }));
  expect(startBody).toBeUndefined();

  const savedRecord = page.locator('.node-record-list > article').filter({ hasText: '测试节点' });
  await expect(savedRecord).toBeVisible();
  await savedRecord.getByRole('button', { name: '启动逐步运行 测试节点' }).click();
  await expect.poll(() => startBody).toEqual(expect.objectContaining({
    startup_mode: 'PROMPT',
    prompt: '读取流程输入并完成节点工作。',
  }));

  await page.getByRole('tab', { name: '直接启动' }).click();
  await expect(page.locator('.node-record-list > article')).toHaveCount(0);
  await page.locator('.run-graph-node').filter({ hasText: '测试节点' }).click();
  await expect(page.locator('.node-console-mode-summary')).toContainText('直接启动');
  await expect(page.getByRole('button', { name: '启动节点会话' })).toBeVisible();
});

test('continuous records never ask users to manually start an auto-ready successor', async ({ page }) => {
  const frozenContext = {
    id: 'context-version-1', capability_type: 'CONTEXT', capability_key: 'delivery-rules',
    digest: 'c'.repeat(64), text: '已冻结的交付规则',
  };
  const contextAsset = {
    ...asset,
    id: 'asset-2',
    name: '测试节点2',
    executor: { ...asset.executor, context_prompt: '节点自定义的执行约束' },
    context_capabilities: [{
      id: frozenContext.id, capability_key: frozenContext.capability_key,
      digest: frozenContext.digest, content_hash: 'd'.repeat(64), text: frozenContext.text,
    }],
  };
  const contextDefinition = {
    ...definition,
    nodes: [definition.nodes[0], { ...definition.nodes[1], asset: contextAsset }],
  };
  const contextSnapshot = { ...snapshot, definition: contextDefinition };
  const autoReadyAttempt = {
    ...attempt,
    id: 'automatic-ready-attempt',
    node_run_id: 'automatic-ready-node-run',
    state: 'WAITING_START_CONFIRMATION',
    runtime_phase: null,
    context_ids: [frozenContext.id],
    frozen_session_contexts: [frozenContext],
    frozen_agent_capabilities: [
      frozenContext,
      { id: 'skill-version-1', capability_type: 'SKILL', capability_key: 'release-check', digest: 'a'.repeat(64) },
      { id: 'mcp-version-1', capability_type: 'MCP', capability_key: 'observability', digest: 'b'.repeat(64) },
    ],
    automatic_progress: {
      stage: 'START_HANDOFF', task_type: 'START_AUTOMATIC_ATTEMPT', task_state: 'RETRY',
      attempts: 2, max_attempts: 5, last_processed_at: now, next_retry_at: '2026-09-01T00:01:00Z',
      task_error: '后台任务执行失败，平台将按重试策略继续处理。', needs_attention: false,
    },
  };
  const autoReadyRecord = {
    ...frozenAutomaticBase,
    active_snapshot_id: contextSnapshot.id,
    snapshots: [contextSnapshot],
    state: 'ACTIVE',
    runtime_status: 'ACTIVE',
    runtime_write_available: true,
    current_node_key: 'second',
    current_node_name: '测试节点2',
    current_attempt_state: 'WAITING_START_CONFIRMATION',
    progress: { accepted: 1, terminal: 1, active: 1 },
    node_runs: [{
      ...nodeRun,
      id: 'automatic-ready-node-run',
      flow_run_id: frozenAutomaticBase.id,
      flow_node_snapshot_key: 'second',
      attempts: [autoReadyAttempt],
    }],
    automation_plan: {
      ...frozenAutomaticBase.automation_plan,
      status: 'FROZEN',
      readiness: { ready: true, issues: [] },
    },
  };
  const attentionAttempt = {
    ...autoReadyAttempt,
    id: 'automatic-attention-attempt',
    node_run_id: 'automatic-attention-node-run',
    automatic_progress: {
      ...autoReadyAttempt.automatic_progress,
      task_state: 'SUCCEEDED', next_retry_at: null, task_error: null, needs_attention: true,
    },
  };
  const attentionRecord = {
    ...autoReadyRecord,
    id: 'automatic-attention',
    name: '自动记录 2',
    node_runs: [{
      ...autoReadyRecord.node_runs[0],
      id: 'automatic-attention-node-run',
      flow_run_id: 'automatic-attention',
      attempts: [attentionAttempt],
    }],
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([autoReadyRecord, attentionRecord]);
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('tab', { name: '连续运行' }).click();
  await page.getByRole('button', { name: '自动记录 1 运行中' }).click();

  const panel = page.locator('.attempt-control');
  const progress = panel.getByTestId('automatic-progress');
  await expect(progress).toContainText('启动节点');
  await expect(progress).toContainText('等待重试');
  await expect(progress).toContainText('后台尝试2 次 / 上限 5');
  await expect(progress).toContainText('下次重试');
  await expect(progress.getByRole('alert')).toContainText('平台将按重试策略继续处理');
  await expect(panel).not.toContainText('正在自动启动');
  await expect(panel).not.toContainText('请在左侧逐步运行记录中点击“启动”');
  const context = panel.locator('.node-context-summary');
  await expect(context).toContainText('节点自定义上下文');
  await expect(context.locator('.node-context-owned')).toContainText('本轮未应用');
  await expect(context.locator('.node-context-repository')).toContainText('本轮已应用');
  await expect(context).toContainText('本轮装配能力2 项');
  await expect(context).toContainText('release-checkSKILL');
  await expect(context).toContainText('observabilityMCP');
  await context.getByRole('button', { name: '查看 delivery-rules' }).click();
  await expect(page.getByRole('dialog', { name: '查看 Context delivery-rules' })).toContainText('已冻结的交付规则');
  await page.getByRole('dialog', { name: '查看 Context delivery-rules' }).getByRole('button', { name: '完成' }).click();

  await page.getByRole('button', { name: '自动记录 2 运行中' }).click();
  await expect(panel.getByTestId('automatic-progress')).toContainText('状态未推进，平台正在自愈');
  await expect(panel.getByRole('status')).toContainText('不需要反复刷新或重新创建运行');
  await expect(page.getByRole('button', { name: /启动连续运行/ })).toHaveCount(0);
});

test('a completed step selects its sole downstream configuration with mapped outputs', async ({ page }) => {
  const transitionDefinition = {
    ...definition,
    nodes: [definition.nodes[0], { ...definition.nodes[1], asset: { ...definition.nodes[1].asset, inputs: [asset.inputs[0]] } }],
  };
  const transitionSnapshot = { ...snapshot, definition: transitionDefinition };
  const mappedArtifact = {
    id: 'n1-output-1', flow_run_id: run.id, producer_attempt_id: 'accepted-attempt-1', consumer_node_key: null,
    field_key: 'output_1', version_no: 1, artifact_type: 'URL', storage_key: null,
    uri: 'https://example.com/n1-output', inline_content: null, content_hash: 'n1-output-hash', byte_size: 27,
    mime_type: 'text/uri-list', source: 'AGENT_OUTPUT', metadata: { display_name: 'N1 输出' }, created_at: now,
  };
  const acceptedAttempt = { ...attempt, id: 'accepted-attempt-1', state: 'ACCEPTED', state_version: 3, artifacts: [mappedArtifact] };
  const acceptedNodeRun = { ...nodeRun, state: 'ACCEPTED', attempts: [acceptedAttempt] };
  const waitingAttempt = {
    ...attempt, id: 'waiting-attempt-2', node_run_id: 'waiting-node-run-2', state: 'WAITING_INPUT', state_version: 1,
    runtime_phase: null, startup_prompt: null, input_bindings: [{ id: 'binding-2', input_field_key: 'input_1', artifact_version_id: mappedArtifact.id, binding_source: 'PORT_MAPPING' }],
  };
  const waitingNodeRun = {
    ...nodeRun, id: 'waiting-node-run-2', flow_node_snapshot_key: 'second', sequence_no: 2,
    state: 'ACTIVE', created_from: 'FLOW_TRANSITION', attempts: [waitingAttempt],
  };
  const configuredAttempt = { ...waitingAttempt, state: 'WAITING_START_CONFIRMATION', state_version: 2, startup_prompt: asset.executor.startup_prompt };
  const configuredNodeRun = { ...waitingNodeRun, attempts: [configuredAttempt] };
  let currentRun = {
    ...run, current_node_key: 'second', current_node_name: '测试节点2', current_attempt_state: 'WAITING_INPUT',
    active_snapshot_id: transitionSnapshot.id, snapshots: [transitionSnapshot],
    progress: { accepted: 1, terminal: 1, active: 1 }, node_runs: [acceptedNodeRun, waitingNodeRun], artifacts: [mappedArtifact],
  };
  let savedBody: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([transitionDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(transitionDefinition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}/nodes/second/runs` && request.method() === 'POST') {
      savedBody = request.postDataJSON() as Record<string, unknown>;
      currentRun = { ...currentRun, current_attempt_state: 'WAITING_START_CONFIRMATION', node_runs: [acceptedNodeRun, configuredNodeRun] };
      return respond(configuredNodeRun, 201);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('button', { name: '测试节点 第 1 次执行 · 已验收' }).click();

  const graph = page.locator('.run-graph');
  await expect(page.locator('.node-record-list > article.active')).toContainText('测试节点2');
  await expect(graph.locator('.run-graph-node.snapshot-selected')).toContainText('测试节点2');
  const consolePanel = page.locator('.node-console');
  await expect(consolePanel).toContainText('上游节点的映射产物已自动填入');
  await expect(consolePanel.getByRole('link', { name: 'https://example.com/n1-output' })).toBeVisible();
  await expect(consolePanel).not.toContainText('尚未填写');

  await consolePanel.getByRole('button', { name: '保存配置' }).click();
  await expect.poll(() => savedBody).toEqual(expect.objectContaining({ artifact_ids: { input_1: mappedArtifact.id } }));
  await expect(page.getByRole('button', { name: '启动逐步运行 测试节点2' })).toBeVisible();
});

test('an accepted legacy step recreates its missing downstream configuration with mapped outputs', async ({ page }) => {
  const transitionDefinition = {
    ...definition,
    nodes: [definition.nodes[0], { ...definition.nodes[1], asset: { ...definition.nodes[1].asset, inputs: [asset.inputs[0]] } }],
  };
  const transitionSnapshot = { ...snapshot, definition: transitionDefinition };
  const mappedArtifact = {
    id: 'legacy-n1-output-1', flow_run_id: run.id, producer_attempt_id: 'legacy-accepted-attempt', consumer_node_key: null,
    field_key: 'output_1', version_no: 1, artifact_type: 'URL', storage_key: null,
    uri: 'https://example.com/legacy-n1-output', inline_content: null, content_hash: 'legacy-output-hash', byte_size: 34,
    mime_type: 'text/uri-list', source: 'AGENT_OUTPUT', metadata: { display_name: 'N1 历史输出' }, created_at: now,
  };
  const acceptedNodeRun = {
    ...nodeRun, state: 'ACCEPTED', accepted_attempt_id: 'legacy-accepted-attempt', attempts: [{
      ...attempt, id: 'legacy-accepted-attempt', state: 'ACCEPTED', state_version: 3, artifacts: [mappedArtifact],
    }],
  };
  const configuredNodeRun = {
    ...nodeRun, id: 'legacy-configured-n2', flow_node_snapshot_key: 'second', sequence_no: 2,
    created_from: 'HUMAN_START', attempts: [{
      ...attempt, id: 'legacy-configured-attempt', node_run_id: 'legacy-configured-n2', state: 'WAITING_START_CONFIRMATION',
      state_version: 1, runtime_phase: null, input_bindings: [{ id: 'legacy-binding', input_field_key: 'input_1', artifact_version_id: mappedArtifact.id, binding_source: 'HUMAN_START' }],
    }],
  };
  let currentRun = {
    ...run, active_snapshot_id: transitionSnapshot.id, snapshots: [transitionSnapshot],
    current_node_key: 'first', current_node_name: '测试节点', current_attempt_state: 'ACCEPTED',
    progress: { accepted: 1, terminal: 1, active: 0 }, node_runs: [acceptedNodeRun], artifacts: [mappedArtifact],
  };
  let savedBody: Record<string, unknown> | undefined;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([transitionDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(transitionDefinition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}/nodes/second/runs` && request.method() === 'POST') {
      savedBody = request.postDataJSON() as Record<string, unknown>;
      currentRun = { ...currentRun, current_node_key: 'second', current_node_name: '测试节点2', current_attempt_state: 'WAITING_START_CONFIRMATION', progress: { accepted: 1, terminal: 1, active: 1 }, node_runs: [acceptedNodeRun, configuredNodeRun] };
      return respond(configuredNodeRun, 201);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('button', { name: '测试节点 第 1 次执行 · 已验收' }).click();

  const consolePanel = page.locator('.node-console');
  await expect(page.locator('.run-graph-node.snapshot-selected')).toContainText('测试节点2');
  await expect(consolePanel.getByRole('link', { name: 'https://example.com/legacy-n1-output' })).toBeVisible();
  await consolePanel.getByRole('button', { name: '保存配置' }).click();
  await expect.poll(() => savedBody).toEqual(expect.objectContaining({ artifact_ids: { input_1: mappedArtifact.id } }));
  await expect(page.getByRole('button', { name: '启动逐步运行 测试节点2' })).toBeVisible();
});

test('run projection stays neutral until record selection and automatic save reports its result', async ({ page }) => {
  let saveRequests = 0;
  let nodeCopyRequests = 0;
  let automaticCopyRequests = 0;
  let nodeCopyBody: Record<string, unknown> | undefined;
  let automaticCopyBody: Record<string, unknown> | undefined;
  let submittedBody: Record<string, unknown> | undefined;
  const capabilityCatalog = Array.from({ length: 31 }, (_, index) => ({
    id: `automatic-skill-${index}`, capability_type: 'SKILL', capability_key: `automatic-skill-${index}`,
    description: `自动运行能力 ${index}`, filename: `automatic-skill-${index}.zip`, is_latest: true, document: {},
  }));
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
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs` && request.method() === 'GET') return respond([frozenAutomaticBase]);
    if (path === '/api/v1/capabilities') return respond(capabilityCatalog);
    if (path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
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
    if (path === `/api/v1/flow-runs/${run.id}/nodes/${nodeRun.id}/copy` && request.method() === 'POST') {
      nodeCopyRequests += 1;
      nodeCopyBody = request.postDataJSON() as Record<string, unknown>;
      return respond({
        ...nodeRun, id: 'node-run-copy', name: nodeCopyBody.name, sequence_no: 2, created_from: 'RECORD_COPY',
        attempts: [{
          ...attempt, id: 'attempt-copy', node_run_id: 'node-run-copy', state: 'WAITING_START_CONFIRMATION',
          state_version: 1, runtime_phase: null, conversation_id: null, output_targets: {}, artifacts: [],
          gate_evaluations: [], error_code: null, error_detail: null,
        }],
      }, 201);
    }
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${automaticBase.id}/copy` && request.method() === 'POST') {
      automaticCopyRequests += 1;
      automaticCopyBody = request.postDataJSON() as Record<string, unknown>;
      return respond({
        ...frozenAutomaticBase, id: 'automatic-copy', run_no: 3, name: automaticCopyBody.name,
        state: 'DRAFT', parent_flow_run_id: run.id, node_runs: [], artifacts: [],
        automation_plan: { ...frozenAutomaticBase.automation_plan, status: 'DRAFT' },
      }, 201);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  const graph = page.locator('.run-graph');
  await expect(page.locator('.run-main .run-title')).toHaveCount(0);
  await expect(page.locator('.run-main h1')).toHaveCount(0);
  await expect(graph.getByText('运行快照 v1', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '返回运行列表' })).toBeVisible();
  await expect(graph).toContainText('未选择逐步运行记录，当前显示中性流程定义');
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);
  await expect(graph.getByText('运行 1 次', { exact: true })).toHaveCount(0);
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(0);

  const manualRecord = page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' });
  await manualRecord.click();
  await expect(graph.locator('.run-graph-node.current')).toContainText('当前激活 · 运行 1 次');
  const selectedGraphNode = graph.locator('.run-graph-node.snapshot-selected');
  await expect(selectedGraphNode).toHaveAttribute('data-selected', 'true');
  const selectedMarker = selectedGraphNode.getByText('已选中', { exact: true });
  const executionLabel = selectedGraphNode.locator('.run-node-execution');
  await expect(selectedMarker).toBeVisible();
  await expect(executionLabel).toHaveText('当前激活 · 运行 1 次');
  const markerBox = await selectedMarker.boundingBox();
  const executionBox = await executionLabel.boundingBox();
  expect(markerBox).not.toBeNull();
  expect(executionBox).not.toBeNull();
  expect(markerBox!.y + markerBox!.height).toBeLessThanOrEqual(executionBox!.y);
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
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(1);
  await page.getByRole('button', { name: '取消当前选择' }).click();
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.locator('.run-rail > h2').click();
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 20 } });
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await expect(graph.getByText('当前激活', { exact: true })).toHaveCount(0);

  await manualRecord.click();
  await page.getByRole('button', { name: '拷贝', exact: true }).click();
  const manualCopyDialog = page.getByRole('dialog', { name: '拷贝逐步运行记录' });
  await expect(manualCopyDialog.getByRole('textbox', { name: '副本名称' })).toHaveValue('测试节点 · 副本');
  await manualCopyDialog.getByRole('textbox', { name: '副本名称' }).fill('测试节点 · 命名副本');
  await manualCopyDialog.getByRole('button', { name: '确认拷贝' }).click();
  await expect.poll(() => nodeCopyRequests).toBe(1);
  expect(nodeCopyBody).toEqual({ name: '测试节点 · 命名副本' });
  await expect(page.locator('.node-record-list .automatic-record-select')).toHaveCount(2);
  await expect(page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点 · 命名副本' })).toBeVisible();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');

  await page.getByRole('tab', { name: '连续运行' }).click();
  await expect(graph).toContainText('未选择连续运行记录，当前显示中性流程定义；请点击左侧“新增”。');
  await page.locator('.run-graph-node').filter({ hasText: '测试节点' }).first().click();
  await expect(page.locator('.run-side-panel')).toHaveCount(0);

  const automaticRecord = page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' });
  await automaticRecord.click();
  const automaticEditor = page.locator('.automatic-record-editor');
  await expect(automaticEditor).toBeVisible();
  await expect(automaticEditor).toHaveAttribute('data-testid', 'node-configuration-panel');
  await expect(automaticEditor.locator('.node-console-mode-summary')).toContainText('连续运行');
  await expect(automaticEditor.getByRole('navigation', { name: '提示词执行配置' })).toContainText('输入与上下文Agent 配置门禁配置执行记录');
  await expect(automaticEditor.getByRole('heading', { name: '输入' })).toBeVisible();
  await expect(automaticEditor.getByRole('link', { name: 'https://example.com/default-input' })).toBeVisible();
  await expect(automaticEditor.getByRole('heading', { name: '启动提示词' })).toBeVisible();
  await expect(automaticEditor).toContainText('读取流程输入并完成节点工作。');
  // Automatic drafts use the same tab-content layout as the manual node console.
  // Their persistence differs, but input and prompt cards must retain the shared
  // spacing and normal block flow rather than a draft-only grid layout.
  const automaticContent = automaticEditor.locator('.action-content');
  await expect(automaticContent).toHaveCSS('display', 'block');
  await expect(automaticEditor.locator('.input-summary')).toHaveCSS('margin-top', '14px');
  await expect(automaticEditor.locator('.startup-prompt-summary')).toHaveCSS('margin-bottom', '12px');

  await automaticEditor.getByRole('button', { name: '填写节点输入' }).click();
  const inputDialog = page.getByRole('dialog', { name: '填写节点输入' });
  await expect(inputDialog.getByRole('textbox', { name: '填写输入 input_1' })).toHaveValue('https://example.com/default-input');
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
  await automaticEditor.locator('.agent-preset-module').first().click();
  const capabilityDialog = page.getByRole('dialog', { name: '配置能力' });
  const capabilityOptions = capabilityDialog.locator('.agent-capability-list > button');
  await expect(capabilityOptions).toHaveCount(31);
  for (let index = 0; index < 31; index += 1) await capabilityOptions.nth(index).click();
  await expect(capabilityDialog).toContainText('已选 31 项');
  await capabilityDialog.getByRole('button', { name: '保存能力' }).click();
  await expect(automaticEditor).toContainText('已选 31 项');
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
    input_urls: {},
  }));
  expect(((submittedBody?.node_plans as Record<string, { agent_preset: Record<string, unknown> }>).first.agent_preset)).not.toHaveProperty('capabilities');
  expect((submittedBody?.node_plans as Record<string, { agent_preset: { capability_version_ids: string[] } }>).first.agent_preset.capability_version_ids).toEqual(capabilityCatalog.map(item => item.id));
  await expect(automaticEditor.getByRole('link', { name: 'https://example.com/input' })).toBeVisible();
  await expect(automaticEditor.getByRole('link', { name: 'example.md' })).toBeVisible();

  await page.getByRole('button', { name: '保存配置' }).click();
  await expect(page.getByRole('alert')).toContainText('保存失败：当前自动运行记录已启动，不能继续修改。');

  await page.getByRole('button', { name: '拷贝', exact: true }).click();
  const automaticCopyDialog = page.getByRole('dialog', { name: '拷贝连续运行记录' });
  await expect(automaticCopyDialog.getByRole('textbox', { name: '副本名称' })).toHaveValue('自动记录 1 · 副本');
  await automaticCopyDialog.getByRole('textbox', { name: '副本名称' }).fill('自动记录 · 命名副本');
  await automaticCopyDialog.getByRole('button', { name: '确认拷贝' }).click();
  await expect.poll(() => automaticCopyRequests).toBe(1);
  expect(automaticCopyBody).toEqual({ name: '自动记录 · 命名副本' });
  await expect(page.locator('.automatic-record-list > article')).toHaveCount(2);
  await expect(page.locator('.automatic-record-list > article.active')).toContainText('自动记录 · 命名副本');
});

test('FR-130 running automatic records show execution facts and chat attempts submit explicit outputs', async ({ page }) => {
  let currentRun = chatRun;
  let submittedOutputs: unknown;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
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

  await page.getByRole('tab', { name: '直接启动' }).click();
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' }).click();
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

  await page.getByRole('tab', { name: '连续运行' }).click();
  await page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' }).click();
  await expect(page.locator('.automatic-record-editor')).toHaveCount(0);
  await expect(page.getByTestId('attempt-state')).toHaveText('END_BLOCKED');
  await expect(page.locator('.attempt-control')).toContainText('连续运行需要人工处理');
  await expect(page.locator('.attempt-control')).toContainText('历史自动流转状态无效');
  await expect(page.locator('.run-graph-node.failed')).toContainText('完成条件未通过');
  await expect(page.locator('.run-graph-node.failed')).toHaveCSS('border-top-color', 'rgb(184, 72, 72)');
  await expect(page.locator('.run-graph-node.automatic-locked')).toContainText('测试节点2');
  await expect(page.locator('.attempt-control').getByRole('button', { name: '取消本轮节点执行' })).toBeVisible();
});

test('continuous record defaults to its final flowed node even after that node fails', async ({ page }) => {
  const thirdNode = {
    id: 'flow-node-3', instance_key: 'third', node_asset_id: 'asset-3', alias: '测试节点3',
    position_x: 880, position_y: 120, config_override: {}, gates: [],
    asset: { ...asset, id: 'asset-3', name: '测试节点3' },
  };
  const threeNodeDefinition = {
    ...definition,
    nodes: [...definition.nodes, thirdNode],
    edges: [...definition.edges, { id: 'edge-2', source_instance_key: 'second', target_instance_key: 'third', position: 1 }],
  };
  const threeNodeSnapshot = { ...snapshot, definition: threeNodeDefinition };
  const completedFirst = {
    ...nodeRun, id: 'automatic-first', flow_run_id: 'automatic-final-node', state: 'ACCEPTED', sequence_no: 1,
    attempts: [{ ...attempt, id: 'automatic-first-attempt', node_run_id: 'automatic-first', snapshot_id: threeNodeSnapshot.id, state: 'ACCEPTED' }],
  };
  const completedSecond = {
    ...nodeRun, id: 'automatic-second', flow_run_id: 'automatic-final-node', flow_node_snapshot_key: 'second', state: 'ACCEPTED', sequence_no: 2,
    attempts: [{ ...attempt, id: 'automatic-second-attempt', node_run_id: 'automatic-second', snapshot_id: threeNodeSnapshot.id, state: 'ACCEPTED' }],
  };
  const failedThird = {
    ...nodeRun, id: 'automatic-third', flow_run_id: 'automatic-final-node', flow_node_snapshot_key: 'third', state: 'FAILED', sequence_no: 3,
    attempts: [{ ...attempt, id: 'automatic-third-attempt', node_run_id: 'automatic-third', snapshot_id: threeNodeSnapshot.id, state: 'END_BLOCKED', error_code: 'AUTOMATIC_GATE_EXECUTION_FAILED' }],
  };
  const finalRecord = {
    ...frozenAutomaticBase,
    id: 'automatic-final-node', name: '第三节点失败记录', state: 'FAILED', active_snapshot_id: threeNodeSnapshot.id,
    snapshots: [threeNodeSnapshot], current_node_key: 'third', current_node_name: '测试节点3', current_attempt_state: 'END_BLOCKED',
    progress: { accepted: 2, terminal: 3, active: 3 }, node_runs: [completedFirst, completedSecond, failedThird],
    automation_plan: { ...frozenAutomaticBase.automation_plan, status: 'FROZEN', reachable_node_keys: ['first', 'second', 'third'], readiness: { ready: true, issues: [] } },
  };
  const finalSummary = {
    // The compact rail endpoint can lag the selected record detail by one
    // polling interval. The selected detail must win, rather than flickering
    // between this stale ACTIVE state and its durable failure state.
    id: finalRecord.id, flow_run_id: run.id, run_no: finalRecord.run_no, name: finalRecord.name, state: 'ACTIVE',
    row_version: finalRecord.row_version, schedule_id: null, schedule_name: null, schedule_occurrence_id: null,
    started_at: now, finished_at: now, plan: { start_node_key: 'first', reachable_node_count: 3, configured_node_count: 3, readiness: { ready: true, issue_count: 0 } },
    progress: { node_runs: 3, accepted: 2, terminal: 3, active: 3 },
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs') return respond([run]);
    if (path === '/api/v1/flows') return respond([threeNodeDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(threeNodeDefinition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/summaries`) return respond([finalSummary]);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${finalRecord.id}`) return respond(finalRecord);
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('tab', { name: '连续运行' }).click();
  await page.locator('.automatic-record-select').filter({ hasText: '第三节点失败记录' }).click();

  const graph = page.locator('.run-graph');
  const third = graph.locator('.run-graph-node').filter({ hasText: '测试节点3' });
  await expect(third).toHaveClass(/failed/);
  await expect(third).toHaveClass(/flow-active-target/);
  await expect(third).toHaveClass(/snapshot-selected/);
  await expect(third).toContainText('当前流转节点');
  await expect(page.getByTestId('attempt-state')).toHaveText('END_BLOCKED');
  await expect(page.locator('.automatic-record-select').filter({ hasText: '第三节点失败记录' })).toContainText('运行失败');

  // Outcome and position remain separate: inspecting an earlier completed
  // node must not erase the durable current-flow marker on the failed node.
  await graph.locator('.run-graph-node').filter({ hasText: '测试节点2' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('ACCEPTED');
  await expect(third).toHaveClass(/flow-active-target/);
});

test('returning from an automatic node session preserves the selected automatic record', async ({ page }) => {
  const conversation = {
    id: 'automatic-conversation-1', display_title: '自动运行会话', title_state: 'MANUAL', lifecycle: 'ACTIVE',
    model_provider_id: null, model_name: null, reasoning_effort: null, streaming_callback_ready: true,
    created_at: now, updated_at: now, last_connected_at: now, capabilities: [],
  };
  await page.routeWebSocket('**/api/v1/flow-runs/**/node-attempts/**/agent-sessions/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
    const respond = (body: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([runningAutomatic]);

    const sessionBase = `/api/v1/flow-runs/${runningAutomatic.id}/node-attempts/${automaticAttempt.id}/agent-sessions`;
    if (path === `${sessionBase}/host`) return respond({
      id: 'automatic-host', display_name: '自动运行节点', desired_state: 'RUNNING', updated_at: now,
    });
    if (path === `${sessionBase}/runtime`) return respond({
      state: 'ACTIVE', write_available: true, message: null, updated_at: now,
    });
    if (path === sessionBase && request.method() === 'GET') return respond([conversation]);
    if (path === `${sessionBase}/work-directories`) return respond({
      root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
    });
    if (path === `${sessionBase}/workspace`) return respond({
      root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
      working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [],
      runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
    });
    if (path === `${sessionBase}/${conversation.id}/events`) return respond({ events: [], cursor: null });
    if (path === `${sessionBase}/${conversation.id}/input-readiness`) return respond({ ready: true, execution_status: 'IDLE' });
    if (path === `${sessionBase}/${conversation.id}/context`) return respond({});
    if (path === `${sessionBase}/${conversation.id}/pending-confirmation`) return respond({ pending: false });
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('tab', { name: '连续运行' }).click();
  await page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' }).click();
  const attemptPanel = page.locator('.attempt-control');
  await expect(attemptPanel).toContainText('END_BLOCKED');
  await attemptPanel.getByRole('button', { name: '进入节点会话' }).click();
  await expect(page.getByRole('button', { name: '返回节点执行' })).toBeVisible();

  // This pushes a nested node-session URL. Its history state must retain the
  // parent FlowRun and automatic-record identity for the explicit return.
  await page.getByRole('button', { name: /自动运行会话/ }).click();
  await expect(page).toHaveURL(new RegExp(`/agent-sessions/${conversation.id}$`));
  await page.getByRole('button', { name: '返回节点执行' }).click();

  await expect(page.getByRole('tab', { name: '连续运行' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.automatic-record-list > article.active')).toContainText('自动记录 1');
  await expect(page.locator('.attempt-control')).toContainText('END_BLOCKED');
});

test('active manual records can be deleted through background cancellation and cleanup', async ({ page }) => {
  let currentRun = run;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
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
  await expect(deleteButton).toBeEnabled();
  await expect(page.getByRole('button', { name: '取消整个流程' })).toHaveCount(0);

  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' }).click();
  await deleteButton.click();
  const deleteDialog = page.getByRole('alertdialog');
  await expect(deleteDialog).toContainText('后台会先取消仍在运行的节点');
  await expect(deleteDialog).toContainText('OpenHands 会话、记录工作区、产物和执行记录');
  await deleteDialog.getByRole('button', { name: '删除', exact: true }).click();

  await expect(page.locator('.node-record-list .automatic-record-select')).toHaveCount(0);
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await page.locator('.run-graph-node').filter({ hasText: '测试节点2' }).click();
  await expect(page.locator('.run-side-panel .node-console')).toBeVisible();
});

test('waiting-input manual records can be deleted without cancellation', async ({ page }) => {
  const waitingInputAttempt = { ...attempt, state: 'WAITING_INPUT', runtime_phase: null };
  let currentRun = {
    ...run,
    current_attempt_state: 'WAITING_INPUT',
    node_runs: [{ ...nodeRun, attempts: [waitingInputAttempt] }],
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: status === 204 ? undefined : JSON.stringify(body),
    });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    if (path === '/api/v1/capabilities' || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}/nodes/${nodeRun.id}` && request.method() === 'DELETE') {
      currentRun = { ...currentRun, node_runs: [], progress: { accepted: 0, terminal: 0, active: 0 } };
      return respond(undefined, 204);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' }).click();

  const deleteButton = page.locator('.manual-record-toolbar').getByRole('button', { name: '删除' });
  await expect(deleteButton).toBeEnabled();
  await deleteButton.click();
  const deleteDialog = page.getByRole('alertdialog');
  await expect(deleteDialog).toContainText('OpenHands 会话、记录工作区、产物和执行记录');
  await deleteDialog.getByRole('button', { name: '删除', exact: true }).click();
  await expect(page.locator('.node-record-list .automatic-record-select')).toHaveCount(0);
});

test('unstarted chat records can be deleted without a cancellation round trip', async ({ page }) => {
  let currentRun = chatRun;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
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
    if (path === `/api/v1/flow-runs/${run.id}/nodes/${chatNodeRun.id}` && request.method() === 'DELETE') {
      currentRun = { ...currentRun, state: 'ACTIVE', progress: { accepted: 0, terminal: 0, active: 0 }, node_runs: [] };
      return respond(undefined, 204);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('tab', { name: '直接启动' }).click();
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' }).click();

  const deleteButton = page.locator('.manual-record-toolbar').getByRole('button', { name: '删除' });
  await expect(deleteButton).toBeEnabled();
  await deleteButton.click();
  const deleteDialog = page.getByRole('alertdialog');
  await expect(deleteDialog).toContainText('OpenHands 会话、记录工作区、产物和执行记录');
  await deleteDialog.getByRole('button', { name: '删除', exact: true }).click();

  await expect(page.locator('.node-record-list .automatic-record-select')).toHaveCount(0);
});

test('manual and automatic records support modifier selection for deletion while copy uses the last click', async ({ page }) => {
  const manualRecords = [
    {
      ...nodeRun, id: 'manual-record-1', name: '手动记录 1', sequence_no: 1,
      attempts: [{ ...attempt, id: 'manual-attempt-1', node_run_id: 'manual-record-1', state: 'WAITING_START_CONFIRMATION', runtime_phase: null }],
    },
    {
      ...nodeRun, id: 'manual-record-2', name: '手动记录 2', sequence_no: 2,
      attempts: [{ ...attempt, id: 'manual-attempt-2', node_run_id: 'manual-record-2', state: 'WAITING_START_CONFIRMATION', runtime_phase: null }],
    },
  ];
  let currentRun = { ...run, node_runs: manualRecords };
  let automaticRecords = [
    { ...frozenAutomaticBase, id: 'automatic-record-1', name: '自动记录 1', schedule_id: 'schedule-1', schedule_name: '每小时检查' },
    { ...frozenAutomaticBase, id: 'automatic-record-2', name: '自动记录 2', run_no: 3, schedule_id: 'schedule-1', schedule_name: '每小时检查' },
  ];
  const deletedManualIds: string[] = [];
  const deletedAutomaticIds: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
    const respond = (body: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: status === 204 ? undefined : JSON.stringify(body),
    });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs` && request.method() === 'GET') return respond(automaticRecords);
    const manualId = manualRecords.find(record => path === `/api/v1/flow-runs/${run.id}/nodes/${record.id}`)?.id;
    if (manualId && request.method() === 'DELETE') {
      deletedManualIds.push(manualId);
      currentRun = { ...currentRun, node_runs: currentRun.node_runs.filter(record => record.id !== manualId) };
      return respond(undefined, 204);
    }
    const automaticId = automaticRecords.find(record => path === `/api/v1/flow-runs/${run.id}/automatic-runs/${record.id}`)?.id;
    if (automaticId && request.method() === 'DELETE') {
      deletedAutomaticIds.push(automaticId);
      automaticRecords = automaticRecords.filter(record => record.id !== automaticId);
      return respond(undefined, 204);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  const manualFirst = page.locator('.node-record-list .automatic-record-select').filter({ hasText: '手动记录 1' });
  const manualSecond = page.locator('.node-record-list .automatic-record-select').filter({ hasText: '手动记录 2' });
  await manualFirst.click();
  await manualSecond.click({ modifiers: ['Meta'] });
  await expect(page.locator('.node-record-list > article.active')).toHaveCount(2);
  const manualDelete = page.locator('.manual-record-toolbar').getByRole('button', { name: '删除 (2)' });
  await expect(manualDelete).toBeEnabled();
  await manualDelete.click();
  const manualDialog = page.getByRole('alertdialog');
  await expect(manualDialog).toContainText('这 2 条记录的 OpenHands 会话、工作区、产物和执行数据');
  await manualDialog.getByRole('button', { name: '删除', exact: true }).click();
  await expect.poll(() => deletedManualIds).toEqual(['manual-record-1', 'manual-record-2']);
  await expect(page.locator('.node-record-list .automatic-record-select')).toHaveCount(0);

  await page.getByRole('tab', { name: '连续运行' }).click();
  const scheduleDirectory = page.locator('.automatic-schedule-directory');
  await expect(scheduleDirectory.getByRole('button', { name: /每小时检查/ })).toHaveAttribute('aria-expanded', 'true');
  await expect(scheduleDirectory.getByRole('button', { name: /删除/ })).toHaveCount(0);
  const automaticFirst = page.locator('.automatic-record-select').filter({ hasText: '自动记录 1' });
  const automaticSecond = page.locator('.automatic-record-select').filter({ hasText: '自动记录 2' });
  await automaticFirst.click();
  await automaticSecond.click({ modifiers: ['Shift'] });
  await expect(page.locator('.automatic-schedule-directory-records > article.active')).toHaveCount(2);
  await page.getByRole('button', { name: '拷贝', exact: true }).click();
  const copyDialog = page.getByRole('dialog', { name: '拷贝连续运行记录' });
  await expect(copyDialog.getByRole('textbox', { name: '副本名称' })).toHaveValue('自动记录 2 · 副本');
  await copyDialog.getByRole('button', { name: '取消' }).click();
  const automaticDelete = page.locator('.automatic-record-toolbar').getByRole('button', { name: '删除 (2)' });
  await automaticDelete.click();
  const automaticDialog = page.getByRole('alertdialog');
  await expect(automaticDialog).toContainText('这 2 条记录的 OpenHands 会话、工作区、产物和执行历史');
  await automaticDialog.getByRole('button', { name: '删除', exact: true }).click();
  await expect.poll(() => deletedAutomaticIds).toEqual(['automatic-record-1', 'automatic-record-2']);
  await expect(page.locator('.automatic-schedule-directory')).toHaveCount(0);
});

test('the step graph keeps the full persisted path while selecting node details', async ({ page }) => {
  const completedFirst = {
    ...nodeRun, id: 'completed-first', name: '已完成的首节点记录', sequence_no: 1, state: 'ACCEPTED',
    attempts: [{ ...attempt, id: 'completed-first-attempt', node_run_id: 'completed-first', state: 'ACCEPTED' }],
  };
  const activeSecond = {
    ...nodeRun, id: 'active-second', name: '正在执行的第二节点记录', flow_node_snapshot_key: 'second', sequence_no: 2,
    attempts: [{ ...attempt, id: 'active-second-attempt', node_run_id: 'active-second', state: 'EXECUTING' }],
  };
  const currentRun = { ...run, node_runs: [completedFirst, activeSecond] };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(authenticatedUser) });
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: path, details: {} } }) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();

  const graphNodes = page.locator('.run-graph-node');
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '已完成的首节点记录' }).click();
  await expect(graphNodes.nth(0)).toContainText('已完成');
  await expect(graphNodes.nth(1)).toContainText('当前激活');

  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '正在执行的第二节点记录' }).click();
  await expect(graphNodes.nth(0)).toContainText('已完成');
  await expect(graphNodes.nth(1)).toContainText('当前激活');
});

test('selecting a historical attempt renders its own frozen graph snapshot', async ({ page }) => {
  const historicalDefinition = {
    ...definition,
    nodes: [
      { ...definition.nodes[0], alias: '历史冻结首节点' },
      { ...definition.nodes[1], alias: '历史冻结次节点' },
    ],
  };
  const activeDefinition = {
    ...definition,
    row_version: 2,
    nodes: [
      { ...definition.nodes[0], alias: '活动版本首节点' },
      { ...definition.nodes[1], alias: '活动版本次节点' },
    ],
  };
  const historicalSnapshot = {
    ...snapshot, id: 'historical-snapshot', version: 1, definition_hash: 'historical-snapshot-hash', definition: historicalDefinition,
  };
  const activeSnapshot = {
    ...snapshot, id: 'active-snapshot', version: 2, definition_hash: 'active-snapshot-hash', definition: activeDefinition,
  };
  const historicalRecord = {
    ...nodeRun, id: 'historical-record', name: '历史执行记录',
    attempts: [{ ...attempt, id: 'historical-attempt', node_run_id: 'historical-record', snapshot_id: historicalSnapshot.id }],
  };
  const currentRun = {
    ...run, active_snapshot_id: activeSnapshot.id, active_snapshot_version: activeSnapshot.version,
    snapshots: [historicalSnapshot, activeSnapshot], node_runs: [historicalRecord],
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([currentRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([activeDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}` && request.method() === 'GET') return respond(currentRun);
    if (path === `/api/v1/flows/${definition.id}`) return respond(activeDefinition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs`) return respond([]);
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: path, details: {} } }) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '历史执行记录' }).click();

  const graph = page.locator('.run-graph');
  await expect(graph).toContainText('历史冻结首节点');
  await expect(graph).toContainText('历史冻结次节点');
  await expect(graph).not.toContainText('活动版本首节点');
  await expect(graph).toContainText('定义 Hash historic');
});

test('completed continuous attempts load frozen input and candidate output metadata on demand', async ({ page }) => {
  const inputArtifact = {
    id: 'completed-input-artifact', flow_run_id: 'automatic-completed', producer_attempt_id: null, consumer_node_key: 'first',
    field_key: 'input_1', version_no: 1, artifact_type: 'URL', storage_key: null,
    uri: 'https://example.com/final-input', inline_content: null, content_hash: 'completed-input-hash', byte_size: 30,
    mime_type: 'text/uri-list', source: 'HUMAN_INPUT', metadata: { display_name: '冻结输入' }, created_at: now,
  };
  const outputArtifact = {
    id: 'completed-output-artifact', flow_run_id: 'automatic-completed', producer_attempt_id: 'completed-attempt', consumer_node_key: null,
    field_key: 'output_1', version_no: 1, artifact_type: 'URL', storage_key: null,
    uri: 'https://example.com/final-output', inline_content: null, content_hash: 'completed-output-hash', byte_size: 31,
    mime_type: 'text/uri-list', source: 'RUNTIME', metadata: { display_name: '已验收输出' }, created_at: now,
  };
  const completedAttempt = {
    ...attempt, id: 'completed-attempt', node_run_id: 'completed-node-run', state: 'ACCEPTED', state_version: 8,
    input_bindings: [{ id: 'completed-input-binding', input_field_key: 'input_1', artifact_version_id: inputArtifact.id, binding_source: 'AUTOMATIC_PORT_MAPPING' }],
    candidate_output_set: { id: 'completed-candidate', completion_event_id: 'completed-event', status: 'GATE_PASSED', artifact_ids: [outputArtifact.id], gate_error_code: null, created_at: now },
    artifacts: [],
  };
  const completedRecord = {
    ...frozenAutomaticBase, id: 'automatic-completed', name: '已完成且保留产物的连续记录', state: 'COMPLETED',
    runtime_status: 'READY', runtime_write_available: false, current_node_key: 'first', current_node_name: '测试节点', current_attempt_state: 'ACCEPTED',
    progress: { accepted: 1, terminal: 1, active: 0 }, artifacts: [],
    node_runs: [{ ...nodeRun, id: 'completed-node-run', flow_run_id: 'automatic-completed', state: 'ACCEPTED', accepted_attempt_id: completedAttempt.id, attempts: [completedAttempt] }],
    automation_plan: { ...frozenAutomaticBase.automation_plan, status: 'FROZEN', readiness: { ready: true, issues: [] } },
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([run]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([definition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${run.id}`) return respond(run);
    if (path === `/api/v1/flows/${definition.id}`) return respond(definition);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/summaries`) return respond([completedRecord]);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${completedRecord.id}`) return respond(completedRecord);
    if (path === `/api/v1/flow-runs/${run.id}/automatic-runs/${completedRecord.id}/artifacts`) {
      const requested = new Set(url.searchParams.getAll('artifact_ids'));
      const items = [inputArtifact, outputArtifact].filter(item => requested.has(item.id));
      return respond({ items, total: items.length, page: 1, page_size: 20 });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: path, details: {} } }) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.getByRole('tab', { name: '连续运行' }).click();
  await page.locator('.automatic-record-select').filter({ hasText: completedRecord.name }).click();

  const panel = page.locator('.attempt-control');
  await expect(panel.getByRole('link', { name: inputArtifact.uri })).toBeVisible();
  await expect(panel.locator('.input-summary article').filter({ hasText: 'input_1' })).not.toContainText('尚未填写');
  await panel.getByRole('button', { name: '输出' }).click();
  const output = panel.getByRole('region', { name: '节点输出' });
  await expect(output).toContainText(outputArtifact.uri);
  await expect(output).not.toContainText('等待本轮执行产出');
});

test('gate review conversation stays compact and remediation enters the created revision', async ({ page }) => {
  const gatePolicy = {
    id: 'gate-policy-1', stage: 'END', position: 0, gate_type: 'PROMPT', enabled: true,
    timeout_seconds: 300, config: { prompt: '检查交付物是否满足验收标准。', code: '' },
    agent_preset: { model_provider_id: 'provider-1', model_name: 'review-model', reasoning_effort: 'medium' },
  };
  const gateEvaluation = {
    id: 'gate-evaluation-1', stage: 'END', policy_snapshot_key: gatePolicy.id, policy_position: 0,
    evaluation_attempt: 1, state: 'COMPLETED', decision: 'FAIL',
    result: { summary: '缺少验收证据', reasons: ['没有附上验证结果'] },
    conversation_available: true, agent_preset: gatePolicy.agent_preset,
    error_code: null, log_excerpt: '', created_at: now,
  };
  const gateAttempt = {
    ...attempt, id: 'gate-attempt-1', state: 'END_BLOCKED', state_version: 5,
    gate_policies: [gatePolicy], gate_evaluations: [gateEvaluation], error_code: null, error_detail: null,
  };
  const gateNodeRun = { ...nodeRun, id: 'gate-node-run-1', attempts: [gateAttempt] };
  const gateDefinition = {
    ...definition,
    nodes: [{ ...definition.nodes[0], gates: [gatePolicy] }, definition.nodes[1]],
  };
  const gateSnapshot = { ...snapshot, definition: gateDefinition };
  const gateRun = {
    ...run, snapshots: [gateSnapshot], node_runs: [gateNodeRun],
    current_attempt_state: 'END_BLOCKED', progress: { accepted: 0, terminal: 1, active: 1 },
  };
  const question = `${'请逐项审查本轮交付内容。'.repeat(12)}提问全文结束标记`;
  const answer = `${'本轮交付缺少可复现的验证证据。'.repeat(12)}回复全文结束标记`;
  const conversationEvents = [
    { id: 'gate-message-user', event_type: 'MESSAGE', payload: { source: 'user', content: '内部审查提示词', display_content: question } },
    { id: 'gate-tool-call', event_type: 'TOOL_CALL', payload: { source: 'agent', tool_name: 'terminal', content: '工具过程不应嵌入审查详情' } },
    { id: 'gate-message-assistant', event_type: 'MESSAGE', payload: { source: 'agent', content: answer } },
  ];
  let releaseRemediation: (() => void) | undefined;
  const remediationPending = new Promise<void>(resolve => { releaseRemediation = resolve; });

  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-runs' && request.method() === 'GET') return respond([gateRun]);
    if (path === '/api/v1/flows' && request.method() === 'GET') return respond([gateDefinition]);
    if (path === '/api/v1/terminal-environments' || path === '/api/v1/capabilities'
      || path === '/api/v1/capability-collections' || path === '/api/v1/model-providers') return respond([]);
    if (path === `/api/v1/flow-runs/${gateRun.id}`) return respond(gateRun);
    if (path === `/api/v1/flows/${gateDefinition.id}`) return respond(gateDefinition);
    if (path === `/api/v1/flow-runs/${gateRun.id}/automatic-runs`) return respond([]);
    if (path === `/api/v1/node-attempts/${gateAttempt.id}/gate-evaluations/${gateEvaluation.id}/conversation/events`) {
      return respond({ events: conversationEvents, next_cursor: null, history_cursor: null });
    }
    if (path === `/api/v1/node-attempts/${gateAttempt.id}/remediate-gate-failure` && request.method() === 'POST') {
      await remediationPending;
      return respond({ attempt: gateAttempt, binding_id: 'gate-revision-binding' }, 201);
    }
    return respond({ error: { code: 'RESOURCE_NOT_FOUND', message: `未配置测试路由：${path}`, details: {} } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '流程运行', exact: true }).click();
  await page.locator('.run-open').click();
  await page.locator('.node-record-list .automatic-record-select').filter({ hasText: '测试节点' }).click();
  const attemptPanel = page.locator('.attempt-control');
  await attemptPanel.getByRole('button', { name: '门禁结果', exact: true }).click();
  await attemptPanel.locator('.gate-overview-row').filter({ hasText: '门禁 1' }).click();
  await page.getByRole('dialog', { name: '门禁 1详情' }).getByRole('button', { name: '查看详情' }).click();

  const gateDialog = page.getByRole('dialog', { name: '门禁详情' });
  const records = gateDialog.getByRole('group', { name: '完整审查问答' });
  await expect(records.getByRole('button')).toHaveCount(2);
  await expect(records).toContainText('审查提问');
  await expect(records).toContainText('审查回复');
  await expect(gateDialog).not.toContainText('工具过程不应嵌入审查详情');
  await expect(gateDialog).not.toContainText('提问全文结束标记');
  await expect(gateDialog).not.toContainText('回复全文结束标记');

  await records.getByRole('button', { name: '查看审查提问完整内容' }).click();
  const questionDialog = page.getByRole('dialog', { name: '审查提问完整内容' });
  await expect(questionDialog.locator('pre')).toHaveText(question);
  await questionDialog.getByRole('button', { name: '关闭审查提问完整内容' }).click();

  await records.getByRole('button', { name: '查看审查回复完整内容' }).click();
  const answerDialog = page.getByRole('dialog', { name: '审查回复完整内容' });
  await expect(answerDialog.locator('pre')).toHaveText(answer);
  await answerDialog.getByRole('button', { name: '关闭审查回复完整内容' }).click();
  await gateDialog.getByRole('button', { name: '关闭门禁详情' }).click();

  await attemptPanel.getByRole('button', { name: '概览', exact: true }).click();
  const remediationButton = attemptPanel.locator('.terminal-run-panel button.primary');
  await expect(remediationButton).toHaveText('根据门禁结果调整并重试');
  await remediationButton.click();
  const confirmation = page.getByRole('alertdialog');
  await expect(confirmation).toContainText('创建成功后直接带你进入该会话');
  await confirmation.getByRole('button', { name: '创建并进入调整会话', exact: true }).click();
  await expect(remediationButton).toContainText('正在创建并进入调整会话');
  await expect(attemptPanel.getByRole('status')).toContainText('正在从本轮完成边界创建调整分支');

  releaseRemediation?.();
  await expect(page).toHaveURL(new RegExp(`/agent-sessions/gate-revision-binding$`));
});
