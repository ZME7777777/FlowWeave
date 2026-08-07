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
  return post(request, '/node-assets', {
    name,
    description: '浏览器端到端验收节点',
    icon_kind: 'LUCIDE',
    icon_value: 'bot',
    inputs: [{ field_key: 'prd', display_name: '需求文档', data_type: 'DOCUMENT', description: '' }],
    outputs: [{ field_key: 'design', display_name: '技术方案', data_type: 'DOCUMENT', description: '' }],
    executor: {
      model_provider_id: null,
      model_name: null,
      startup_prompt: '读取输入并生成方案',
      context_prompt: '保留证据',
      timeout_seconds: 120,
      max_iterations: 20,
    },
    capabilities: [skill],
    default_skill_ref: skill.capability_key,
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
    default_entry_key: 'design_a',
    nodes: [
      { instance_key: 'design_a', node_asset_id: assetId, alias: '首轮方案', position_x: 100, position_y: 160, config_override: {}, gates },
      { instance_key: 'design_b', node_asset_id: assetId, alias: '复核方案', position_x: 500, position_y: 160, config_override: {}, gates },
    ],
    edges: [{
      source_instance_key: 'design_a',
      target_instance_key: 'design_b',
      position: 0,
      mappings: [{ source_output_key: 'design', target_input_key: 'prd' }],
    }],
  });
}

async function login(page: Page) {
  await page.goto('/');
  await page.evaluate(() => localStorage.removeItem('flowweave-workbench'));
  await page.reload();
}

async function connect(source: Locator, target: Locator) {
  const from = source.locator('.react-flow__handle.source').first();
  const to = target.locator('.react-flow__handle.target').first();
  await from.dragTo(to);
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
  const skillInput = editor.locator('label.file-button').filter({ hasText: '导入 Skill ZIP' }).locator('input');
  await skillInput.setInputFiles('e2e/fixtures/ui-product-skill.zip');
  await expect(editor.getByTestId('capability-key')).toHaveText('ui-product-skill');
  await editor.getByRole('button', { name: '下一步' }).click();
  await expect(editor.getByRole('heading', { name: '输入字段' })).toBeVisible();
  await expect(editor.getByRole('heading', { name: '输出字段' })).toBeVisible();
  await expect(editor.locator('.io-empty')).toHaveCount(2);
  await editor.getByRole('button', { name: '添加输入' }).click();
  await editor.getByRole('button', { name: '添加输出' }).click();
  await expect(editor.getByLabel('inputs key 0')).toHaveValue('input_1');
  await expect(editor.getByLabel('outputs key 0')).toHaveValue('output_1');
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
  await assetButton.dragTo(canvas, { targetPosition: { x: 320, y: 260 } });
  await assetButton.dragTo(canvas, { targetPosition: { x: 700, y: 360 } });
  await expect(canvas.getByRole('status')).toContainText('再次添加');
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await connect(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await canvas.locator('.react-flow__node').nth(1).getByRole('button', { name: `删除节点 ${assetName}` }).click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(canvas.getByRole('status')).toContainText('关联连线');
  await assetButton.dragTo(canvas, { targetPosition: { x: 700, y: 360 } });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await connect(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await page.getByRole('button', { name: '自动布局' }).click();
  await canvas.locator('.react-flow__node').nth(0).click();
  await page.getByRole('button', { name: '添加开始门禁' }).click();
  await page.getByRole('button', { name: '添加结束门禁' }).click();
  await expect(page.locator('.gate-row')).toHaveCount(2);
  await page.getByRole('button', { name: `删除节点 ${assetName}` }).last().click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(page.getByLabel('默认入口')).not.toHaveValue('');
  await assetButton.dragTo(canvas, { targetPosition: { x: 320, y: 260 } });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await page.getByLabel('流程名称').fill(`UI流程-${suffix}`);
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
  await dialog.getByLabel('开始节点').selectOption('design_a');
  await dialog.getByLabel('需求文档').fill('端到端需求文档 v1');
  await dialog.getByRole('button', { name: '创建运行' }).click();

  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');
  await expect(page.locator('.gate-results')).toContainText('START');

  const graphNodes = page.locator('.run-graph .react-flow__node');
  await graphNodes.filter({ hasText: '复核方案' }).click();
  const pendingNodeDialog = page.getByRole('dialog', { name: '运行节点详情 复核方案' });
  await expect(pendingNodeDialog).toContainText('未激活');
  await expect(pendingNodeDialog).toContainText('ui-product-skill');
  await expect(pendingNodeDialog).toContainText('START · #1');
  await expect(pendingNodeDialog).toContainText('END · #1');
  await pendingNodeDialog.getByRole('button', { name: '关闭运行节点详情' }).click();

  await graphNodes.filter({ hasText: '首轮方案' }).click();
  const activeNodeDialog = page.getByRole('dialog', { name: '运行节点详情 首轮方案' });
  await expect(activeNodeDialog).toContainText('运行次数');
  await expect(activeNodeDialog).toContainText('WAITING_START_CONFIRMATION');
  await expect(activeNodeDialog).toContainText('1 轮');
  await activeNodeDialog.getByRole('button', { name: '查看最新运行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');

  const changedFlow = await request.put(`${apiBase}/api/v1/flows/${flow.id}`, {
    data: {
      name: flow.name,
      description: '运行中发布的新流程配置',
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
      edges: flow.edges.map((edge: { source_instance_key: string; target_instance_key: string; position: number; mappings: Array<{ source_output_key: string; target_input_key: string }> }) => ({
        source_instance_key: edge.source_instance_key,
        target_instance_key: edge.target_instance_key,
        position: edge.position,
        mappings: edge.mappings,
      })),
    },
  });
  expect(changedFlow.ok(), await changedFlow.text()).toBeTruthy();
  await expect(page.getByTestId('snapshot-sync')).toBeVisible();
  await page.getByRole('button', { name: '同步最新配置' }).click();
  await expect(page.locator('.run-title')).toContainText('流程快照 v2');
  await expect(page.getByTestId('snapshot-sync')).toBeHidden();

  await page.getByLabel('重新运行节点').selectOption('design_b');
  await page.getByRole('button', { name: '从此节点运行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_INPUT');
  await page.getByLabel('绑定输入 prd').selectOption({ index: 1 });
  await page.getByRole('button', { name: '保存输入绑定' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');
  await expect(page.locator('.timeline button')).toHaveCount(2);

  await page.locator('.timeline button').first().click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');
  await page.getByRole('button', { name: '确认开始执行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_ACCEPTANCE');
  await expect(page.locator('.artifacts')).toContainText('design');
  await page.getByRole('button', { name: '预览' }).click();
  const preview = page.getByRole('dialog', { name: '产物预览' });
  await expect(preview).toContainText('Mock output');
  await preview.getByRole('button', { name: '关闭产物预览' }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('design-v1.txt');
  await page.getByLabel('验收意见').fill('补充恢复策略');
  await page.getByRole('button', { name: /退回修改并进入第 2 轮/ }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');
  await expect(page.locator('.attempt-tabs')).toContainText('第 1 轮');
  await expect(page.locator('.attempt-tabs')).toContainText('第 2 轮');
  await page.getByRole('button', { name: '确认开始执行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_ACCEPTANCE');
  await page.getByRole('button', { name: '确认完成' }).click();
  await expect(page.locator('.timeline button')).toHaveCount(3);
  await expect(page.getByTestId('attempt-state')).toHaveText('WAITING_START_CONFIRMATION');
  await expect(page.locator('.binding-row')).toContainText('prd');

  await page.getByRole('button', { name: '返回运行列表' }).click();
  const summaryGroup = page.locator('.run-group').filter({ hasText: flow.name });
  await expect(summaryGroup).toContainText('复核方案');
  await expect(summaryGroup).toContainText('1 已完成 / 1 终态 / 3 已激活');
  await expect(summaryGroup).toContainText('需要人工处理');
  await page.getByLabel('搜索流程或运行').fill(flow.name);
  await expect(page.locator('.run-group')).toHaveCount(1);
  await page.getByLabel('运行状态筛选').selectOption('WAITING_HUMAN');
  await expect(summaryGroup).toBeVisible();
  await summaryGroup.getByRole('button', { name: `收起 ${flow.name}` }).click();
  await expect(summaryGroup.locator('.run-table')).toBeHidden();
  await summaryGroup.getByRole('button', { name: `展开 ${flow.name}` }).click();
  await expect(summaryGroup.locator('.run-table')).toBeVisible();

  const flows = await request.get(`${apiBase}/api/v1/flows`);
  expect(flows.ok()).toBeTruthy();
  const detail = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect(detail.ok()).toBeTruthy();
  expect((await detail.json()).some((item: { flow_definition_id: string }) => item.flow_definition_id === flow.id)).toBeTruthy();
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
  await expect(page.locator('.timeline button')).toHaveCount(3);

  page.once('dialog', dialog => {
    expect(dialog.message()).toContain('所有尚未结束的节点运行都会被取消');
    dialog.accept();
  });
  await page.getByRole('button', { name: '取消整个流程' }).click();
  await expect(page.getByTestId('flow-run-state')).toHaveText('已取消');
  await expect(page.locator('.run-progress')).toContainText('0 已验收');
  await expect(page.locator('.run-progress')).toContainText('3 已结束');
  await expect(page.locator('.run-progress')).toContainText('0 进行中');
  await expect(page.locator('.terminal-run-panel')).toContainText('流程已取消');
  await expect(page.getByRole('button', { name: '从此节点运行' })).toBeHidden();
  await expect(page.getByRole('button', { name: '取消整个流程' })).toBeHidden();

  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '永久删除此运行' }).click();
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
  })).toBe(1);
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
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: `删除节点资产 ${singleAsset.name}` }).click();
  await expect(page.getByTestId('node-card')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '批量删除 (2)' }).click();
  await expect(page.getByTestId('node-card')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已删除 2 个节点资产');

  // Flow definitions: preserve runs while supporting one-row and filtered bulk deletion.
  await page.getByRole('button', { name: '流程编排' }).click();
  const flowLibrary = page.getByTestId('flow-library');
  await flowLibrary.getByLabel('搜索流程').fill(`${marker}-流程`);
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  page.once('dialog', dialog => dialog.accept());
  await flowLibrary.getByRole('button', { name: `删除流程 ${singleFlow.name}` }).click();
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(2);
  await flowLibrary.getByRole('button', { name: '全选' }).click();
  await expect(flowLibrary.getByRole('button', { name: '删除 (2)' })).toBeEnabled();
  page.once('dialog', dialog => dialog.accept());
  await flowLibrary.getByRole('button', { name: '删除 (2)' }).click();
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(0);

  // Runs remain discoverable from snapshots after their definitions are deleted.
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.getByLabel('搜索流程或运行').fill(`${marker}-运行`);
  await expect(page.locator('.run-row')).toHaveCount(3);
  await expect(page.locator('.deleted-resource')).toHaveCount(3);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: `删除运行 ${marker}-运行-单删` }).click();
  await expect(page.locator('.run-row')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '批量删除 (2)' }).click();
  await expect(page.locator('.run-row')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已永久删除 2 个流程运行');

  const listed = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect(listed.ok()).toBeTruthy();
  const remainingIds = new Set((await listed.json()).map((run: { id: string }) => run.id));
  for (const run of runs) expect(remainingIds.has(run.id)).toBeFalsy();
  expect(bulkAssetA.id).toBeTruthy();
  expect(bulkAssetB.id).toBeTruthy();
});
