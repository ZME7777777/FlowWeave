import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ id: 'node-editor-user', username: 'node-editor-user', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true }),
  }));
});

const node = {
  id: '00000000-0000-0000-0000-000000000101',
  directory_id: null,
  name: '节点编辑步骤回归',
  description: '验证下一步不会提前保存',
  icon_kind: 'LUCIDE',
  icon_value: 'bot',
  workspace_ref: 'node-assets/00000000-0000-0000-0000-000000000101',
  row_version: 1,
  inputs: [],
  outputs: [],
  executor: { startup_prompt: '启动提示词', context_prompt: '上下文提示词', context_capability_ids: [] },
  context_capabilities: [],
  created_at: '2026-08-30T00:00:00Z',
  updated_at: '2026-08-30T00:00:00Z',
};

test('clicking a node record shows its editor in the detail panel', async ({ page }) => {
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([node]),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).click();

  await expect(page.locator('.node-detail-toolbar-label')).toContainText(node.name);
  await expect(page.locator('.node-center-detail .node-detail-editor')).toBeVisible();
  await expect(page.locator('.modal-backdrop')).toHaveCount(0);
});

test('editor shows all configuration sections without saving', async ({ page }) => {
  let updateCount = 0;
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => {
    if (route.request().method() === 'PUT') {
      updateCount += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(node) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([node]) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).click();

  await expect(page.getByLabel('节点名称')).toBeVisible();
  await expect(page.getByRole('button', { name: '查看和编辑启动触发提示词' })).toBeVisible();
  await expect(page.getByText('输入定义', { exact: true })).toBeVisible();
  await expect(page.getByText('输出定义', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存节点' })).toBeVisible();
  expect(updateCount).toBe(0);
});

test('copy button creates a configured node copy without opening its editor', async ({ page }) => {
  const configuredNode = {
    ...node,
    inputs: [{ field_key: 'source_url', display_name: '来源链接', data_type: 'URL', description: '用于读取原始数据' }],
    outputs: [{ field_key: 'report', display_name: '分析报告', data_type: 'FILE', description: '生成的报告文件' }],
  };
  let createPayload: Record<string, unknown> | undefined;
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', async route => {
    if (route.request().method() === 'POST') {
      createPayload = route.request().postDataJSON() as Record<string, unknown>;
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ ...configuredNode, id: 'node-copy', name: '节点编辑步骤回归 副本' }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([configuredNode]) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: `复制节点 ${node.name}` }).click();

  await expect.poll(() => createPayload).toMatchObject({
    name: '节点编辑步骤回归 副本',
    inputs: configuredNode.inputs,
    outputs: configuredNode.outputs,
    executor: configuredNode.executor,
  });
  await expect(page.getByText('节点“节点编辑步骤回归 副本”已复制。')).toBeVisible();
  await expect(page.getByRole('heading', { name: '编辑节点' })).toHaveCount(0);
});

test('prompt blocks open full editing and Escape cancels only the detail dialog', async ({ page }) => {
  let saved: Record<string, unknown> | undefined;
  await page.route('**/api/v1/node-directories', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/node-assets**', route => {
    if (route.request().method() === 'PUT') {
      saved = route.request().postDataJSON();
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...node, ...saved }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([node]) });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').click();
  const preview = page.getByRole('button', { name: '查看和编辑上下文提示词' });
  await preview.click();
  const dialog = page.getByRole('dialog', { name: '上下文提示词', exact: true });
  await expect(dialog.getByLabel('上下文提示词', { exact: true })).toBeFocused();
  await dialog.getByLabel('上下文提示词', { exact: true }).fill('取消的上下文');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(preview).toContainText(node.executor.context_prompt);
  await expect(page.getByLabel('节点名称')).toBeVisible();
  await expect(preview).toBeFocused();
  await preview.click();
  await dialog.getByLabel('上下文提示词', { exact: true }).fill('应用后的完整上下文\n第二行内容');
  await dialog.screenshot({ path: test.info().outputPath('prompt-editor.png') });
  await dialog.getByRole('button', { name: '应用', exact: true }).click();
  await expect(preview).toContainText('应用后的完整上下文');
  expect(saved).toBeUndefined();
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect.poll(() => saved).toMatchObject({ executor: { ...node.executor, context_prompt: '应用后的完整上下文\n第二行内容' } });
});

test('input and output rows configure fields in dialogs and save one complete node payload', async ({ page }) => {
  const configured = { ...node, inputs: [{ field_key: 'source', display_name: '来源', data_type: 'FILE', description: '原始来源' }], outputs: [{ field_key: 'report', display_name: '报告', data_type: 'URL', description: '结果链接' }] };
  let writes = 0;
  let saved: Record<string, unknown> | undefined;
  await page.route('**/api/v1/node-directories', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/node-assets**', route => {
    if (route.request().method() === 'PUT') {
      writes += 1; saved = route.request().postDataJSON();
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...configured, ...saved }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([configured]) });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').click();
  const inputRow = page.getByRole('button', { name: '配置输入定义' });
  const outputRow = page.getByRole('button', { name: '配置输出定义' });
  await expect(inputRow).toContainText('来源');
  await expect(outputRow).toContainText('报告');
  await inputRow.click();
  const inputs = page.getByRole('dialog', { name: '输入定义', exact: true });
  await expect(inputs.getByLabel('inputs key 0')).toHaveValue('source');
  await inputs.getByRole('button', { name: '添加输入', exact: true }).click();
  await inputs.getByLabel('inputs key 1').fill('source');
  await inputs.getByRole('button', { name: '应用', exact: true }).click();
  await expect(inputs.getByRole('alert')).toContainText('字段标识不能重复');
  await inputs.getByLabel('inputs key 1').fill('evidence');
  await inputs.getByLabel('inputs name 1').fill('证据索引');
  await inputs.getByLabel('inputs description 1').fill('输入说明');
  await inputs.getByRole('radiogroup', { name: 'inputs type 1' }).getByRole('radio', { name: '文件', exact: true }).click();
  await inputs.screenshot({ path: test.info().outputPath('input-fields-editor.png') });
  await inputs.getByRole('button', { name: '应用', exact: true }).click();
  await expect(inputRow).toContainText('2 个字段');
  await outputRow.click();
  const outputs = page.getByRole('dialog', { name: '输出定义', exact: true });
  await outputs.getByLabel('outputs name 0').fill('已取消的修改');
  await outputs.getByRole('button', { name: '取消', exact: true }).click();
  await expect(outputRow).toContainText('报告');
  await expect(outputRow).not.toContainText('已取消');
  expect(writes).toBe(0);
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect.poll(() => saved).toMatchObject({ inputs: [...configured.inputs, { field_key: 'evidence', display_name: '证据索引', data_type: 'FILE', description: '输入说明' }], outputs: configured.outputs, executor: configured.executor, row_version: 1 });
  expect(writes).toBe(1);
});

for (const width of [1440, 1024]) {
  test(`compact node form keeps a narrow rail, fixed prompt blocks and green creation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const longNode = { ...node, executor: { ...node.executor, startup_prompt: '逐项核对输入，保留完整证据与执行记录。\n'.repeat(35) }, inputs: Array.from({ length: 6 }, (_, i) => ({ field_key: `source_${i}`, display_name: `很长的输入产物名称 ${i}`, data_type: 'FILE', description: '字段说明' })) };
    await page.route('**/api/v1/node-directories', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'directory-1', name: '研发流程', parent_id: null, position: 0 }]) }));
    await page.route('**/api/v1/node-assets**', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([longNode]) }));
    await page.goto('/');
    await page.getByRole('button', { name: '节点中心', exact: true }).click();
    await page.getByTestId('node-card').click();
    const add = page.getByRole('button', { name: '新建未分类节点' });
    await add.hover();
    const color = await add.evaluate(element => getComputedStyle(element).color);
    expect(color).toBe('rgb(20, 90, 64)');
    const rail = await page.getByRole('complementary', { name: '节点目录' }).boundingBox();
    expect(rail!.width).toBeLessThanOrEqual(width === 1440 ? 241 : 217);
    const geometry = await page.locator('.node-center-detail').evaluate(element => ({ scroll: element.scrollWidth, width: element.clientWidth }));
    expect(geometry.scroll).toBe(geometry.width);
    const previews = await page.locator('.node-prompt-preview').evaluateAll(elements => elements.map(element => ({ height: element.getBoundingClientRect().height, overflow: element.scrollHeight > element.clientHeight })));
    expect(previews.map(item => item.height)).toEqual([76, 76]);
    await expect(page.locator('.node-fields-summary')).toHaveCount(2);
    await expect(page.locator('.node-form-body select')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '保存节点', exact: true })).toBeVisible();
    const directory = page.getByRole('combobox', { name: '所属目录', exact: true });
    await directory.focus();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await expect(directory).toContainText('研发流程');
    await expect(page.getByRole('listbox', { name: '所属目录' })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`compact-node-${width}.png`) });
  });
}
