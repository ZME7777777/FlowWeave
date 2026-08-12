import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

const apiBase = process.env.E2E_API_URL ?? 'http://127.0.0.1:8080';
const suffix = Date.now().toString(36);

async function post(request: APIRequestContext, path: string, data: unknown) {
  const response = await request.post(`${apiBase}/api/v1${path}`, { data });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}


async function importSkill(request: APIRequestContext) {
  const validated = await post(request, '/capability-imports/validate', {
    capability_type: 'SKILL',
    filename: 'ui-product-skill.zip',
    content_base64: Buffer.from(await import('node:fs').then(fs => fs.readFileSync('e2e/fixtures/ui-product-skill.zip'))).toString('base64'),
  });
  const committed = await post(request, '/capability-imports', { import_token: validated.import_token });
  return committed.capabilities[0];
}

async function createAsset(request: APIRequestContext, name: string) {
  const skill = await importSkill(request);
  const modelName = `e2e-model-${suffix}`;
  const provider = await post(request, '/model-providers', {
    name: `E2E模型服务-${name}`,
    base_url: 'http://127.0.0.1:9/v1',
    api_key: 'e2e-placeholder-key',
    models: [{ model_name: modelName, enabled: true, is_default: true }],
  });
  return post(request, '/node-assets', {
    name,
    description: '浏览器端到端验收节点',
    icon_kind: 'LUCIDE',
    icon_value: 'bot',
    inputs: [{ field_key: 'prd', display_name: '需求文档', data_type: 'URL', description: '', template_url: '' }],
    outputs: [{ field_key: 'design', display_name: '技术方案', data_type: 'URL', description: '', template_url: '' }],
    executor: {
      model_provider_id: provider.id,
      model_name: modelName,
      startup_prompt: '读取输入并生成方案',
      context_prompt: '保留证据',
      timeout_seconds: 120,
      max_iterations: 20,
    },
    capabilities: [skill],
  });
}

async function createFlow(request: APIRequestContext, assetId: string, name: string) {
  const gates = [
    { stage: 'START', position: 0, gate_type: 'JAVASCRIPT', enabled: true, timeout_seconds: 30, config: { code: "return {decision: 'PASS', summary: '开始门禁通过', reasons: [], evidence: [], details: {}};" } },
    { stage: 'END', position: 0, gate_type: 'PYTHON', enabled: true, timeout_seconds: 30, config: { code: "result = {'decision': 'PASS', 'summary': '结束门禁通过', 'reasons': [], 'evidence': [], 'details': {}}" } },
  ];
  return post(request, '/flows', {
    name,
    description: '同一资产重复放置并显式映射产物',
    lark_root_folder_url: 'https://example.feishu.cn/drive/folder/e2e-flow-root',
    default_entry_key: 'design_a',
    nodes: [
      { instance_key: 'design_a', node_asset_id: assetId, alias: '首轮方案', position_x: 100, position_y: 160, config_override: {}, gates },
      { instance_key: 'design_b', node_asset_id: assetId, alias: '复核方案', position_x: 500, position_y: 160, config_override: {}, gates },
    ],
    edges: [{
      source_instance_key: 'design_a',
      target_instance_key: 'design_b',
      position: 0,
    }],
    port_mappings: [{ source_instance_key: 'design_a', source_output_key: 'design', target_instance_key: 'design_b', target_input_key: 'prd' }],
  });
}

async function login(page: Page) {
  await page.goto('/');
  await page.evaluate(() => localStorage.removeItem('flowweave-workbench'));
  await page.reload();
}

async function confirmProductDialog(
  page: Page,
  trigger: Locator,
  confirmLabel: string,
  message?: string,
) {
  await trigger.click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  if (message) await expect(dialog).toContainText(message);
  await dialog.getByRole('button', { name: confirmLabel, exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function connectFlow(source: Locator, target: Locator) {
  const from = source.locator('.flow-direction-handle.source');
  const to = target.locator('.flow-direction-handle.target');
  await from.dragTo(to);
}

async function connectArtifact(source: Locator, target: Locator) {
  const from = source.locator('.data-port-handle.source').first();
  const to = target.locator('.data-port-handle.target').first();
  await from.dragTo(to);
}

async function dropAsset(page: Page, asset: Locator, canvas: Locator, position: { x: number; y: number }) {
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  const event = {
    dataTransfer,
    clientX: (box?.x ?? 0) + position.x,
    clientY: (box?.y ?? 0) + position.y,
  };
  await asset.dispatchEvent('dragstart', event);
  await canvas.dispatchEvent('dragover', event);
  await canvas.dispatchEvent('drop', event);
  await dataTransfer.dispose();
}

test('node asset editor and repeated flow-node canvas match the product model', async ({ page }) => {
  await login(page);

  const providerName = `UI模型服务-${suffix}`;
  await page.getByRole('button', { name: '大模型配置' }).click();
  await page.getByRole('button', { name: '新增模型服务' }).click();
  const providerEditor = page.locator('form.model-editor');
  await providerEditor.getByLabel('服务名称').fill(providerName);
  await providerEditor.getByLabel('Base URL').fill('https://models.example.test/v1');
  await providerEditor.getByLabel('模型 1').fill('gpt-e2e');
  await providerEditor.getByRole('button', { name: '保存模型服务' }).click();
  const providerCard = page.locator('.model-config-card').filter({ hasText: providerName });
  await expect(providerCard).toContainText('可用于节点');
  await expect(providerCard).toContainText('0 个');
  await page.route('**/api/v1/model-providers/*/test', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ connection_state: 'CONNECTED', model_count: 1 }),
  }));
  await providerCard.getByRole('button', { name: '测试连接' }).click();
  await expect(providerCard.getByRole('status')).toContainText('连接成功，服务返回 1 个模型');
  await page.unroute('**/api/v1/model-providers/*/test');

  await page.getByRole('button', { name: '能力仓库' }).click();
  await expect(page.getByRole('heading', { name: '能力仓库', exact: true })).toBeVisible();
  const skillInput = page.locator('label.file-button').filter({ hasText: '上传 Skill ZIP' }).locator('input');
  await skillInput.setInputFiles('e2e/fixtures/ui-product-skill.zip');
  const importDialog = page.getByRole('alertdialog', { name: '确认导入 ui-product-skill.zip' });
  await expect(importDialog).toContainText('识别到 1 个 Skill');
  const committedImport = page.waitForResponse(response => response.url().endsWith('/api/v1/capability-imports') && response.request().method() === 'POST');
  await importDialog.getByRole('button', { name: '导入 1 项能力', exact: true }).click();
  expect((await committedImport).ok()).toBeTruthy();
  await expect(page.getByRole('status')).toContainText('已从 ui-product-skill.zip 导入 1 项 Skill 能力');
  await expect(page.locator('.capability-card').filter({ hasText: 'ui-product-skill' }).first()).toBeVisible();

  await page.getByRole('button', { name: '节点资产' }).click();
  await expect(page.getByRole('heading', { name: '节点资产', exact: true })).toBeVisible();

  const assetName = `UI节点资产-${suffix}`;
  await page.getByRole('button', { name: '新建节点' }).click();
  const editor = page.locator('form.asset-editor');
  await editor.getByLabel('节点名称').fill(assetName);
  await editor.getByLabel('节点说明').fill('四步节点资产编辑器验收');
  await editor.getByRole('button', { name: '下一步' }).click();
  await editor.getByLabel('模型服务').selectOption({ label: providerName });
  await editor.getByLabel('模型', { exact: true }).selectOption('gpt-e2e');
  await editor.getByLabel('启动触发提示词').fill('读取输入并执行节点任务');
  await editor.getByRole('button', { name: '下一步' }).click();
  const selectedSkill = editor.getByLabel('选择能力 ui-product-skill').first();
  await selectedSkill.check();
  await expect(selectedSkill).toBeChecked();
  await editor.getByRole('button', { name: '下一步' }).click();
  await expect(editor.getByRole('heading', { name: '输入定义' })).toBeVisible();
  await expect(editor.getByRole('heading', { name: '输出定义' })).toBeVisible();
  await expect(editor.locator('.io-empty')).toHaveCount(2);
  await editor.getByRole('button', { name: '添加输入' }).click();
  await editor.getByRole('button', { name: '添加输出' }).click();
  await expect(editor.getByLabel('inputs key 0')).toHaveValue('input_1');
  await expect(editor.getByLabel('outputs key 0')).toHaveValue('output_1');
  await editor.getByLabel('inputs name 0').fill('输入产物');
  await editor.getByLabel('outputs name 0').fill('输出产物');
  const card = page.getByTestId('node-card').filter({ hasText: assetName }).last();
  const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/node-assets') && response.request().method() === 'POST');
  await editor.evaluate((form: HTMLFormElement) => form.requestSubmit());
  expect((await saved).ok()).toBeTruthy();
  await expect(card).toBeVisible();
  await card.click();
  const detail = page.getByRole('dialog', { name: `节点详情 ${assetName}` });
  await expect(detail).toContainText('ui-product-skill');
  await detail.getByRole('button', { name: '关闭节点详情' }).click();

  await page.getByRole('button', { name: '大模型配置' }).click();
  await expect(providerCard).toContainText('1 个');
  await expect(providerCard).toContainText('可用于节点');

  await page.getByRole('button', { name: '流程编排' }).click();
  await page.getByRole('button', { name: '新建流程' }).click();
  const library = page.getByTestId('flow-library');
  const assetButton = library.getByRole('button', { name: assetName, exact: true }).last();
  const canvas = page.getByTestId('flow-designer');
  await expect(canvas.locator('.react-flow__pane')).toBeVisible();
  await dropAsset(page, assetButton, canvas, { x: 320, y: 260 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await dropAsset(page, assetButton, canvas, { x: 700, y: 360 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await expect(canvas.getByRole('status')).toContainText('再次添加');
  await canvas.getByRole('button', { name: '流程走向' }).click();
  await connectFlow(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await canvas.locator('.react-flow__node').nth(1).getByRole('button', { name: `删除节点 ${assetName}` }).click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(canvas.getByRole('status')).toContainText('关联连线');
  await dropAsset(page, assetButton, canvas, { x: 700, y: 360 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await connectFlow(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await canvas.getByRole('button', { name: '产物流转' }).click();
  await connectArtifact(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(2);
  await page.getByRole('button', { name: '自动布局' }).click();
  await canvas.locator('.react-flow__node').nth(0).click();
  await page.getByRole('button', { name: '添加开始门禁' }).click();
  await page.getByRole('button', { name: '添加结束门禁' }).click();
  await expect(page.locator('.gate-row')).toHaveCount(2);
  await page.getByRole('button', { name: `删除节点 ${assetName}` }).last().click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(page.getByLabel('默认入口')).toHaveValue('');
  await dropAsset(page, assetButton, canvas, { x: 320, y: 260 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await page.getByLabel('流程名称').fill(`UI流程-${suffix}`);
  await page.getByLabel('飞书 Wiki 根节点').fill('https://example.feishu.cn/wiki/e2e-ui-root');
  await expect(page.getByRole('button', { name: '保存流程' })).toBeEnabled();
  const flowSaved = page.waitForResponse(response => response.url().endsWith('/api/v1/flows') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存流程' }).click();
  const flowResponse = await flowSaved;
  expect(flowResponse.ok(), await flowResponse.text()).toBeTruthy();
  await expect(library.getByRole('button', { name: `UI流程-${suffix}`, exact: true })).toBeVisible();
});

test('run keeps attempts, snapshots, gates and artifact lineage visible', async ({ page, request }) => {
  const asset = await createAsset(request, `运行资产-${suffix}`);
  const flow = await createFlow(request, asset.id, `运行流程-${suffix}`);
  await login(page);
  await page.getByRole('button', { name: '流程运行' }).click();
  const runGroup = page.locator('.run-group').filter({ hasText: flow.name });
  await expect(runGroup).toBeVisible();
  await runGroup.getByRole('button', { name: '启动', exact: true }).click();
  const dialog = page.locator('form.start-run-modal');
  await dialog.getByLabel('运行名称').fill(`运行验收-${suffix}`);
  await dialog.getByLabel('终端镜像').selectOption({ index: 1 });
  const createdRunResponse = page.waitForResponse(response => response.url().endsWith(`/api/v1/flows/${flow.id}/runs`) && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '创建运行' }).click();
  const createdRun = await (await createdRunResponse).json();
  await expect(page.getByText('点击任意节点，在右侧配置输入并开始一次独立执行', { exact: true })).toBeVisible();
  await expect(page.locator('.action-panel')).toHaveCount(0);
  const graphNodes = page.locator('.run-graph .react-flow__node');
  await graphNodes.filter({ hasText: '首轮方案' }).click();
  const nodeConsole = page.locator('.node-console');
  await expect(nodeConsole).toContainText('首轮方案');
  await expect(nodeConsole).toContainText('创建一次独立执行');
  await expect(nodeConsole).toContainText('使用 Skill 启动');
  await expect(nodeConsole).toContainText('发送启动提示词');
  await expect(nodeConsole).toContainText('仅创建会话启动');

  await nodeConsole.getByText('新建输入产物', { exact: true }).click();
  await nodeConsole.getByLabel('新建产物名称 prd').fill(`需求文档-${suffix}`);
  await nodeConsole.getByLabel('新建产物 URL prd').fill(`https://example.feishu.cn/docx/e2e-input-${suffix}`);
  await nodeConsole.getByRole('button', { name: '保存到产物池' }).click();
  await expect(nodeConsole.locator('.selected-artifact')).toContainText(`需求文档-${suffix}`);
  await expect(nodeConsole.locator('.selected-artifact')).toContainText(`https://example.feishu.cn/docx/e2e-input-${suffix}`);
  await nodeConsole.getByRole('button', { name: '开始第 1 次执行' }).click();

  const attemptControl = page.locator('.attempt-control');
  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await graphNodes.filter({ hasText: '首轮方案' }).click();
  await expect(page.locator('.run-graph .run-graph-node.snapshot-selected')).toHaveCount(1);
  await page.locator('.run-rail .timeline').getByRole('button', { name: /首轮方案/ }).click();
  await expect(page.locator('.run-graph .run-graph-node.snapshot-selected')).toHaveCount(0);
  await expect(attemptControl).toContainText('首轮方案');
  const agentChatEntry = attemptControl.getByRole('button', { name: '进入 Agent 对话' });
  await expect(agentChatEntry).toBeVisible();
  await expect(attemptControl.locator('.attempt-runtime-summary')).toHaveCount(0);
  expect(await attemptControl.evaluate(panel => {
    const state = panel.querySelector('.state-banner');
    const chat = panel.querySelector('.agent-chat-entry');
    const frozenInputs = Array.from(panel.querySelectorAll('.attempt-side-section')).find(section => section.textContent?.includes('本轮冻结输入'));
    return Boolean(state && chat && frozenInputs
      && (state.compareDocumentPosition(chat) & Node.DOCUMENT_POSITION_FOLLOWING)
      && (chat.compareDocumentPosition(frozenInputs) & Node.DOCUMENT_POSITION_FOLLOWING));
  })).toBeTruthy();
  await expect(attemptControl.locator('.attempt-input-card')).toContainText(`需求文档-${suffix}`);
  await expect(attemptControl.locator('.attempt-input-card')).toContainText(`https://example.feishu.cn/docx/e2e-input-${suffix}`);
  await expect(attemptControl.locator('.gate-results')).toContainText('START');

  const changedFlow = await request.put(`${apiBase}/api/v1/flows/${flow.id}`, {
    data: {
      name: flow.name,
      description: '运行中发布的新流程配置',
      lark_root_folder_url: flow.lark_root_folder_url,
      default_entry_key: flow.default_entry_key,
      row_version: flow.row_version,
      nodes: flow.nodes.map((node: { instance_key: string; node_asset_id: string; alias?: string | null; position_x: number; position_y: number; config_override: Record<string, unknown>; gates: Array<{ stage: string; position: number; gate_type: string; enabled: boolean; timeout_seconds: number; config: Record<string, unknown> }> }) => ({
        instance_key: node.instance_key,
        node_asset_id: node.node_asset_id,
        alias: node.alias,
        position_x: node.position_x,
        position_y: node.position_y,
        config_override: node.config_override,
        gates: node.gates.map(gate => ({
          stage: gate.stage, position: gate.position, gate_type: gate.gate_type,
          enabled: gate.enabled, timeout_seconds: gate.timeout_seconds, config: gate.config,
        })),
      })),
      edges: flow.edges.map((edge: { source_instance_key: string; target_instance_key: string; position: number }) => ({
        source_instance_key: edge.source_instance_key,
        target_instance_key: edge.target_instance_key,
        position: edge.position,
      })),
      port_mappings: flow.port_mappings.map((mapping: {
        source_instance_key: string; source_output_key: string;
        target_instance_key: string; target_input_key: string;
      }) => ({
        source_instance_key: mapping.source_instance_key,
        source_output_key: mapping.source_output_key,
        target_instance_key: mapping.target_instance_key,
        target_input_key: mapping.target_input_key,
      })),
    },
  });
  expect(changedFlow.ok(), await changedFlow.text()).toBeTruthy();
  await expect(page.getByTestId('snapshot-sync')).toBeVisible();
  await page.getByRole('button', { name: '同步最新配置' }).click();
  await expect(page.locator('.run-title')).toContainText('流程快照 v2');
  await expect(page.getByTestId('snapshot-sync')).toBeHidden();

  await attemptControl.getByRole('button', { name: '创建新的独立执行' }).click();
  await expect(nodeConsole).toContainText('已有执行记录');
  await nodeConsole.getByLabel('节点输入 prd').selectOption({ index: 1 });
  await nodeConsole.getByRole('button', { name: '开始第 2 次执行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await expect(page.locator('.timeline button')).toHaveCount(2);
  await expect(attemptControl.getByRole('button', { name: '取消整个流程' })).toHaveCount(0);
  await expect(page.locator('.flow-run-management').getByRole('button', { name: '取消整个流程' })).toBeVisible();
  await confirmProductDialog(
    page,
    attemptControl.getByRole('button', { name: '取消本轮节点执行' }),
    '取消本轮执行',
    '其他节点执行和整个流程不会被取消',
  );
  await expect(page.getByTestId('attempt-state')).toHaveText('CANCELLED');
  await expect(page.getByTestId('flow-run-state')).toHaveText('运行中');

  const runResponse = await request.get(`${apiBase}/api/v1/flow-runs/${createdRun.id}`);
  expect(runResponse.ok(), await runResponse.text()).toBeTruthy();
  const run = await runResponse.json();
  expect(run.environment_version?.image_digest).toMatch(/^sha256:/);
  expect(run.artifacts).toEqual(expect.arrayContaining([expect.objectContaining({
    field_key: 'prd',
    uri: `https://example.feishu.cn/docx/e2e-input-${suffix}`,
    source: 'HUMAN',
    metadata: expect.objectContaining({
      source: 'HUMAN_INPUT',
      display_name: `需求文档-${suffix}`,
    }),
  })]));
  expect(run.node_runs[0].attempts[0].input_bindings).toEqual(expect.arrayContaining([
    expect.objectContaining({ input_field_key: 'prd' }),
  ]));
  expect(run.node_runs.filter((item: { flow_node_snapshot_key: string }) => item.flow_node_snapshot_key === 'design_a')).toHaveLength(2);
  expect(run.state).not.toBe('CANCELLED');
  expect(run.node_runs).toEqual(expect.arrayContaining([
    expect.objectContaining({ state: 'ACTIVE' }),
    expect.objectContaining({ state: 'CANCELLED' }),
  ]));
});

test('cancelled run becomes read-only and can be permanently deleted', async ({ page, request }) => {
  const asset = await createAsset(request, `终态资产-${suffix}`);
  const flow = await createFlow(request, asset.id, `终态流程-${suffix}`);
  const started = await post(request, `/flows/${flow.id}/runs`, { name: `终态运行-${suffix}` });
  await post(request, `/flow-runs/${started.id}/nodes/design_a/runs`, { artifact_ids: {} });
  await post(request, `/flow-runs/${started.id}/nodes/design_b/runs`, { artifact_ids: {} });

  await login(page);
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.getByLabel('搜索流程或运行').fill(`终态运行-${suffix}`);
  await page.locator('.run-open').click();
  await expect(page.locator('.timeline button')).toHaveCount(2);
  await page.locator('.timeline button').first().click();

  await confirmProductDialog(
    page,
    page.locator('.flow-run-management').getByRole('button', { name: '取消整个流程' }),
    '取消流程',
    '未结束的执行会被取消',
  );
  await expect(page.getByTestId('flow-run-state')).toHaveText('已取消');
  await expect(page.locator('.run-progress')).toContainText('0 已验收');
  await expect(page.locator('.run-progress')).toContainText('2 已结束');
  await expect(page.locator('.run-progress')).toContainText('0 进行中');
  await expect(page.locator('.terminal-run-panel')).toContainText('流程已取消');
  await expect(page.getByRole('button', { name: '创建新的独立执行' })).toBeHidden();
  await expect(page.getByRole('button', { name: '取消整个流程' })).toBeHidden();

  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '永久删除此运行' }),
    '永久删除',
  );
  await expect(page.getByRole('heading', { name: '流程运行', exact: true })).toBeVisible();
  const listed = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect((await listed.json()).some((item: { id: string }) => item.id === started.id)).toBeFalsy();
});

test('corrupt and legacy browser state recover without a blank page', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto('/');

  await page.evaluate(() => localStorage.setItem('flowweave-workbench', '{broken-json'));
  await page.reload();
  await expect(page.getByRole('heading', { name: '节点资产', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('flowweave-workbench'))).toBeNull();

  await page.evaluate(() => localStorage.setItem('flowweave-workbench', JSON.stringify({
    version: 0,
    state: { view: 'legacy-dashboard', selectedRunId: 42, selectedNodeRunId: false },
  })));
  await page.reload();
  await expect(page.getByRole('heading', { name: '节点资产', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const raw = localStorage.getItem('flowweave-workbench');
    return raw ? JSON.parse(raw).version : undefined;
  })).toBe(2);
  const persisted = await page.evaluate(() => JSON.parse(localStorage.getItem('flowweave-workbench') ?? '{}'));
  expect(persisted.state).toEqual({ view: 'nodes' });
  expect(pageErrors).toEqual([]);
});

test('node assets, flows and runs support single and filtered bulk deletion', async ({ page, request }) => {
  const marker = `删除管理-${Date.now().toString(36)}`;
  const singleAsset = await createAsset(request, `${marker}-节点-单删`);
  const bulkAssetA = await createAsset(request, `${marker}-节点-批量-A`);
  const bulkAssetB = await createAsset(request, `${marker}-节点-批量-B`);
  const flowAsset = await createAsset(request, `${marker}-流程依赖节点`);
  const singleFlow = await createFlow(request, flowAsset.id, `${marker}-流程-单删`);
  const bulkFlowA = await createFlow(request, flowAsset.id, `${marker}-流程-批量-A`);
  const bulkFlowB = await createFlow(request, flowAsset.id, `${marker}-流程-批量-B`);
  const runs = await Promise.all([
    post(request, `/flows/${singleFlow.id}/runs`, { name: `${marker}-运行-单删` }),
    post(request, `/flows/${bulkFlowA.id}/runs`, { name: `${marker}-运行-批量-A` }),
    post(request, `/flows/${bulkFlowB.id}/runs`, { name: `${marker}-运行-批量-B` }),
  ]);

  await login(page);

  // Node assets: one explicit row action and filtered-result bulk deletion.
  await page.getByLabel('搜索节点').fill(`${marker}-节点`);
  await expect(page.getByTestId('node-card')).toHaveCount(3);
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: `删除节点资产 ${singleAsset.name}` }),
    '确认删除',
  );
  await expect(page.getByTestId('node-card')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '批量删除 (2)' }),
    '确认删除',
  );
  await expect(page.getByTestId('node-card')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已删除 2 个节点资产');

  // Flow definitions with runs are protected until their runs are deleted.
  await page.getByRole('button', { name: '流程编排' }).click();
  const flowLibrary = page.getByTestId('flow-library');
  await flowLibrary.getByLabel('搜索流程').fill(`${marker}-流程`);
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: `删除流程 ${singleFlow.name}` }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await expect(page.locator('.canvas-error')).toContainText('请先删除关联运行');

  // Delete runs before permanently deleting their flow definitions.
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.getByLabel('搜索流程或运行').fill(`${marker}-运行`);
  await expect(page.locator('.run-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: `删除运行 ${marker}-运行-单删` }),
    '永久删除',
  );
  await expect(page.locator('.run-row')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '批量删除 (2)' }),
    '永久删除',
  );
  await expect(page.locator('.run-row')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已永久删除 2 个流程运行');

  await page.getByRole('button', { name: '流程编排' }).click();
  await flowLibrary.getByLabel('搜索流程').fill(`${marker}-流程`);
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: `删除流程 ${singleFlow.name}` }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(2);
  await flowLibrary.getByRole('button', { name: '全选' }).click();
  await expect(flowLibrary.getByRole('button', { name: '删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: '删除 (2)' }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(0);

  const listed = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect(listed.ok()).toBeTruthy();
  const remainingIds = new Set((await listed.json()).map((run: { id: string }) => run.id));
  for (const run of runs) expect(remainingIds.has(run.id)).toBeFalsy();
  expect(bulkAssetA.id).toBeTruthy();
  expect(bulkAssetB.id).toBeTruthy();
});
