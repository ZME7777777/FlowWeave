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
  await expect(page.getByRole('button', { name: '查看启动触发提示词全文' })).toBeVisible();
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
  const preview = page.getByRole('button', { name: '查看上下文提示词全文' });
  await preview.click();
  const dialog = page.getByRole('dialog', { name: '上下文提示词', exact: true });
  await expect(dialog.getByLabel('上下文提示词', { exact: true })).toBeFocused();
  await dialog.getByLabel('上下文提示词', { exact: true }).fill('取消的上下文');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.node-prompt-field').last()).toContainText(node.executor.context_prompt);
  await expect(page.getByLabel('节点名称')).toBeVisible();
  await expect(preview).toBeFocused();
  await preview.click();
  await dialog.getByLabel('上下文提示词', { exact: true }).fill('应用后的完整上下文\n第二行内容');
  await dialog.screenshot({ path: test.info().outputPath('prompt-editor.png') });
  await dialog.getByRole('button', { name: '应用', exact: true }).click();
  await expect(page.locator('.node-prompt-field').last()).toContainText('应用后的完整上下文');
  expect(saved).toBeUndefined();
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect.poll(() => saved).toMatchObject({ executor: { ...node.executor, context_prompt: '应用后的完整上下文\n第二行内容' } });
});

test('field list saves create, edit and delete immediately without persisting other drafts', async ({ page }) => {
  let current = { ...node, inputs: [{ field_key: 'source', display_name: '来源', data_type: 'FILE', description: '原始来源' }], outputs: [{ field_key: 'report', display_name: '报告', data_type: 'URL', description: '结果链接' }] };
  const writes: Record<string, unknown>[] = [];
  let reject = false;
  await page.route('**/api/v1/node-directories', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/node-assets**', route => {
    if (route.request().method() === 'PUT') {
      const payload = route.request().postDataJSON();
      if (reject) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'VERSION_CONFLICT', message: '保存失败，请重试。' } }) });
      expect(payload.row_version).toBe(current.row_version);
      writes.push(payload); current = { ...current, ...payload, row_version: current.row_version + 1 };
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(current) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([current]) });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').click();
  await page.getByLabel('节点名称').fill('尚未保存的节点名');
  await page.getByRole('button', { name: '配置输入定义' }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '应用', exact: true })).toHaveCount(0);
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toHaveCount(0);
  const row = dialog.locator('.node-field-record').first();
  await expect(row).toContainText('来源'); await expect(row).toContainText('原始来源');
  expect((await row.boundingBox())!.height).toBeLessThanOrEqual(46);
  await dialog.getByRole('button', { name: '添加输入', exact: true }).click();
  await dialog.getByLabel('输入名称', { exact: true }).fill('证据索引');
  await dialog.getByLabel('输入说明', { exact: true }).fill('输入说明');
  await dialog.getByLabel('输入字段标识', { exact: true }).fill('source');
  await dialog.getByRole('button', { name: '创建字段', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('字段标识不能重复');
  await dialog.getByLabel('输入字段标识', { exact: true }).fill('evidence');
  await dialog.getByRole('radio', { name: '文件', exact: true }).click();
  reject = true;
  await dialog.getByRole('button', { name: '创建字段', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('保存失败');
  await expect(dialog.getByLabel('输入名称', { exact: true })).toHaveValue('证据索引');
  reject = false;
  await dialog.getByRole('button', { name: '创建字段', exact: true }).click();
  await expect(dialog.getByRole('table')).toBeVisible();
  expect(writes).toHaveLength(1); expect(writes[0].name).toBe(node.name);
  await expect(page.getByLabel('节点名称')).toHaveValue('尚未保存的节点名');
  await expect(dialog.locator('.node-field-record')).toHaveCount(2);
  await dialog.getByRole('button', { name: '修改输入 证据索引' }).click();
  await dialog.getByLabel('输入说明', { exact: true }).fill('修订后的说明');
  await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
  await expect(dialog.getByRole('table')).toContainText('修订后的说明');
  expect(writes).toHaveLength(2);
  reject = true;
  await dialog.getByRole('button', { name: '删除输入 来源' }).click();
  await expect(dialog.getByRole('alert')).toContainText('保存失败');
  await expect(dialog.locator('.node-field-record')).toHaveCount(2);
  reject = false;
  await dialog.getByRole('button', { name: '删除输入 来源' }).click();
  await expect(dialog.locator('.node-field-record')).toHaveCount(1);
  expect(writes).toHaveLength(3);
  await dialog.screenshot({ path: test.info().outputPath('compact-fields-list.png') });
  await dialog.getByRole('button', { name: '关闭输入定义', exact: true }).click();
  await page.getByRole('button', { name: '配置输出定义' }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '配置输出 报告' }).click();
  await dialog.getByLabel('输出名称', { exact: true }).fill('已编辑的报告');
  await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
  await expect(dialog.getByRole('table')).toContainText('已编辑的报告');
  expect(writes).toHaveLength(4);
  await dialog.getByRole('button', { name: '关闭输出定义', exact: true }).click();
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect.poll(() => writes.length).toBe(5);
  expect(writes[4]).toMatchObject({ name: '尚未保存的节点名', inputs: [{ field_key: 'evidence', display_name: '证据索引', description: '修订后的说明', data_type: 'FILE' }], outputs: [{ display_name: '已编辑的报告' }] });
  await page.reload();
  await page.getByTestId('node-card').click();
  await page.getByRole('button', { name: '配置输入定义' }).click();
  await expect(page.getByRole('dialog').getByRole('table')).toContainText('修订后的说明');
});

test('first field creates a named draft node once and keeps the list open', async ({ page }) => {
  let current: typeof node | undefined;
  let creates = 0;
  await page.route('**/api/v1/node-directories', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/node-assets**', route => {
    if (route.request().method() === 'POST') {
      creates += 1; current = { ...node, ...route.request().postDataJSON(), row_version: 1 };
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(current) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(current ? [current] : []) });
  });
  await page.goto('/'); await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByRole('button', { name: '新建未分类节点' }).click();
  await page.getByLabel('节点名称').fill('新建并即时保存字段');
  await page.getByRole('button', { name: '配置输入定义' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '添加输入', exact: true }).click();
  await dialog.getByLabel('输入名称', { exact: true }).fill('源文件');
  await dialog.getByRole('button', { name: '创建字段', exact: true }).click();
  await expect(dialog.getByRole('table')).toContainText('源文件'); expect(creates).toBe(1);
  await expect(page.getByTestId('node-card')).toContainText('新建并即时保存字段');
});

for (const [width, height] of [[1440, 900], [1440, 700], [1024, 768], [1280, 620]]) {
  test(`compact node form keeps a narrow rail, fixed prompt blocks and green creation at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
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
    expect(rail!.width).toBeLessThanOrEqual(width > 1024 ? 241 : 217);
    const geometry = await page.locator('.node-center-detail').evaluate(element => ({ scroll: element.scrollWidth, width: element.clientWidth }));
    expect(geometry.scroll).toBe(geometry.width);
    await expect(page.locator('.node-prompt-field')).toHaveCount(2);
    await expect(page.locator('.node-prompt-field').getByRole('button')).toHaveCount(2);
    await expect(page.locator('.node-prompt-field svg.lucide-maximize-2')).toHaveCount(0);
    const boxes = await page.locator('.node-center-detail, .node-detail-editor, .node-form-body').evaluateAll(elements => elements.map(element => ({ height: element.clientHeight, scroll: element.scrollHeight })));
    expect(boxes.every(box => box.scroll <= box.height + 1)).toBe(true);
    await page.locator('.node-center-detail').hover(); await page.mouse.wheel(0, 900);
    expect(await page.locator('.node-center-detail').evaluate(element => element.scrollTop)).toBe(0);
    expect(await page.evaluate(() => ({ scroll: document.documentElement.scrollHeight, height: innerHeight, top: scrollY })) ).toEqual({ scroll: height, height, top: 0 });
    await expect(page.getByRole('button', { name: '查看上下文提示词全文' })).toBeVisible();
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
    await page.screenshot({ path: test.info().outputPath(`compact-node-${width}-${height}.png`) });
  });
}
