import { expect, test, type Page } from '@playwright/test';
import type { NodeAsset, NodeAssetWrite } from '../src/types';

const node: NodeAsset = {
  id: '00000000-0000-0000-0000-000000000101', directory_id: null,
  name: '批量代码修复', description: '按应用处理已分析异常，保留修复与验证证据。',
  icon_kind: 'LUCIDE', icon_value: 'bot', row_version: 1, inputs: [], outputs: [],
  executor: { startup_prompt: '# 执行步骤\n\n1. 读取输入\n2. 修复并验证\n', context_prompt: '保留原始证据与工作区内容。', context_capability_ids: [] },
  context_capabilities: [], created_at: '2026-08-30T00:00:00Z', updated_at: '2026-08-30T00:00:00Z',
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id: 'node-editor-user', username: 'node-editor-user', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true }) }));
});
async function setup(page: Page, initial: NodeAsset[] = [node], directory = false) {
  let records = structuredClone(initial);
  const writes: NodeAssetWrite[] = [];
  let reject = false;
  let release: (() => void) | undefined;
  let hold = false;
  await page.route('**/api/v1/node-directories', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(directory ? [{ id: 'directory-1', name: '研发流程', parent_id: null, position: 0 }] : []) }));
  await page.route('**/api/v1/node-assets**', async route => {
    const method = route.request().method();
    if (method === 'DELETE') {
      const ids = route.request().postDataJSON().ids as string[];
      records = records.filter(item => !ids.includes(item.id));
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ deleted_ids: ids, blocked: [] }) });
    }
    if (method === 'POST' || method === 'PUT') {
      const payload = route.request().postDataJSON() as NodeAssetWrite;
      writes.push(payload);
      if (hold) await new Promise<void>(resolve => { release = resolve; });
      if (reject) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'VERSION_CONFLICT', message: '节点已被其他人修改，请刷新后重试。' } }) });
      const id = method === 'POST' ? 'created-node' : new URL(route.request().url()).pathname.split('/').at(-1)!;
      const previous = records.find(item => item.id === id);
      if (previous) expect(payload.row_version).toBe(previous.row_version);
      const value = { ...node, ...payload, id, row_version: (previous?.row_version ?? 0) + 1 } as NodeAsset;
      for (const direction of ['inputs', 'outputs'] as const) value[direction] = value[direction].map((field, i) => ({ ...field, field_key: field.field_key || `${direction}_${i}` }));
      records = [...records.filter(item => item.id !== id), value];
      return route.fulfill({ status: method === 'POST' ? 201 : 200, contentType: 'application/json', body: JSON.stringify(value) });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(records) });
  });
  await page.goto('/'); await page.getByRole('button', { name: '节点中心', exact: true }).click();
  return { writes, reject: (value: boolean) => { reject = value; }, hold: () => { hold = true; }, release: () => { hold = false; release?.(); } };
}
const openNode = async (page: Page, name = node.name) => page.getByTestId('node-card').filter({ hasText: name }).click();
const openFields = async (page: Page) => page.getByRole('tab', { name: /输入与输出/ }).click();

async function expectSaved(page: Page) {
  await expect(page.getByRole('button', { name: '保存节点', exact: true })).toBeEnabled();
  await expect(page.locator('.node-edit-workspace > header').getByRole('button', { name: '取消', exact: true })).toBeDisabled();
}

test('selecting a node opens a compact workspace with full inline prompts', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  await expect(page.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: '启动提示词', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab')).toHaveCount(3);
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue(node.executor!.startup_prompt);
  await expect(page.locator('.node-center-detail .node-detail-editor')).toBeVisible();
  await expect(page.locator('.modal-backdrop')).toHaveCount(0);
  await expect(page.locator('.node-center-detail')).not.toContainText('NODE FORM');
  await openFields(page);
  await expect(page.getByRole('region', { name: '输入定义' })).toBeVisible();
  await expect(page.getByRole('region', { name: '输出定义' })).toBeVisible();
  expect(state.writes).toHaveLength(0);
});

test('created metadata shares one read-only header without save status', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  const header = page.locator('.node-edit-workspace > header');
  await expect(header.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
  await expect(header.locator('p')).toHaveText(node.description);
  await expect(header.locator('p')).toHaveAttribute('title', node.description);
  await expect(page.getByRole('textbox', { name: '节点名称', exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '节点说明', exact: true })).toHaveCount(0);
  await expect(page.locator('.node-save-state')).toHaveCount(0);
  await expect(header.getByRole('button', { name: '关闭', exact: true })).toHaveCount(0);
  await expect(header.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
  const geometry = await header.evaluate(element => { const title = element.querySelector('h2')!.getBoundingClientRect(); const description = element.querySelector('p')!.getBoundingClientRect(); return Math.abs((title.top + title.height / 2) - (description.top + description.height / 2)); });
  expect(geometry).toBeLessThan(2);
  expect(state.writes).toHaveLength(0);
});

test('prompt tabs, Markdown preview and enlarged editor share the same draft', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('# 任务\n\n- 核对证据\n- **完成验证**');
  await page.getByRole('button', { name: '预览', exact: true }).click();
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible();
  await expect(page.locator('.node-prompt-preview strong')).toHaveText('完成验证');
  await page.getByRole('tab', { name: '上下文', exact: true }).click();
  await page.getByRole('textbox', { name: '上下文提示词', exact: true }).fill('新的执行约束');
  const expand = page.getByRole('button', { name: '放大编辑上下文提示词' });
  await expand.click();
  let dialog = page.getByRole('dialog', { name: '上下文提示词', exact: true });
  await expect(dialog.getByRole('textbox')).toBeFocused();
  await dialog.getByRole('textbox').fill('放大编辑的完整上下文\n第二行');
  await dialog.getByRole('button', { name: '退出放大编辑', exact: true }).click();
  await expect(dialog).toHaveCount(0); await expect(expand).toBeFocused();
  await expect(page.getByRole('textbox', { name: '上下文提示词', exact: true })).toHaveValue('放大编辑的完整上下文\n第二行');
  await expand.click(); dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox').fill('外部关闭仍保留草稿');
  await page.locator('.node-expanded-backdrop').click({ position: { x: 2, y: 2 } });
  await expect(dialog).toHaveCount(0);
  expect(state.writes).toHaveLength(0);
  await page.getByRole('tab', { name: '启动提示词', exact: true }).click();
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).press('ControlOrMeta+s');
  await expectSaved(page);
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].executor).toMatchObject({ startup_prompt: '# 任务\n\n- 核对证据\n- **完成验证**', context_prompt: '外部关闭仍保留草稿' });
});

test('input and output creation editing and deletion remain drafts until the one node save', async ({ page }) => {
  const configured = { ...node, inputs: [{ field_key: 'source', display_name: '来源', data_type: 'FILE' as const, description: '原始来源' }], outputs: [{ field_key: 'report', display_name: '报告', data_type: 'URL' as const, description: '结果链接' }] };
  const state = await setup(page, [configured]); await openNode(page);

  await openFields(page);
  await page.getByRole('button', { name: '新增输入', exact: true }).click();
  await page.getByLabel('输入名称 2', { exact: true }).fill('证据索引');
  await page.getByLabel('输入说明 2', { exact: true }).fill('修订后的完整说明');
  await page.getByRole('group', { name: '输入类型 2', exact: true }).getByRole('button', { name: '文件', exact: true }).click();
  await page.getByRole('button', { name: '删除输入 来源', exact: true }).click();
  await expect(page.getByLabel('输入名称 1', { exact: true })).toHaveValue('证据索引');
  await expect(page.getByLabel('输入说明 1', { exact: true })).toHaveValue('修订后的完整说明');
  await page.getByLabel('输出名称 1', { exact: true }).fill('已编辑的报告');
  await page.getByLabel('输出说明 1', { exact: true }).fill('报告交付要求');
  expect(state.writes).toHaveLength(0);
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expectSaved(page);
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({ name: node.name, row_version: 1, inputs: [{ display_name: '证据索引', description: '修订后的完整说明', data_type: 'FILE' }], outputs: [{ field_key: 'report', display_name: '已编辑的报告', description: '报告交付要求' }] });
  expect(state.writes[0].inputs[0]).not.toHaveProperty('draftId');
  await page.reload(); await openNode(page); await openFields(page);
  await expect(page.getByLabel('输入说明 1', { exact: true })).toHaveValue('修订后的完整说明');
});

test('new nodes can configure fields before a name and save only once in their directory', async ({ page }) => {
  const state = await setup(page, [], true);
  await page.getByRole('button', { name: '在 研发流程 新建节点', exact: true }).click();
  await openFields(page); await page.getByRole('button', { name: '新增输入', exact: true }).click();
  await page.getByLabel('输入名称 1', { exact: true }).fill('源文件');
  await page.getByRole('button', { name: '新增输出', exact: true }).click();
  await page.getByLabel('输出名称 1', { exact: true }).fill('报告');
  expect(state.writes).toHaveLength(0);
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('请填写节点名称');
  await expect(page.getByLabel('节点名称', { exact: true })).toBeFocused();
  await page.getByLabel('节点名称', { exact: true }).fill('先配置字段的新节点');
  await page.getByLabel('节点说明', { exact: true }).fill('创建时填写的用途');
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expectSaved(page);
  expect(state.writes).toHaveLength(1);
  await expect(page.getByLabel('节点名称', { exact: true })).toHaveCount(0);
  await expect(page.locator('.node-fixed-metadata')).toContainText('创建时填写的用途');
  expect(state.writes[0]).toMatchObject({ name: '先配置字段的新节点', description: '创建时填写的用途', directory_id: 'directory-1', inputs: [{ display_name: '源文件' }], outputs: [{ display_name: '报告' }], executor: { startup_prompt: '', context_prompt: '' } });
});

test('invalid hidden fields are revealed and version conflicts preserve every draft for retry', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  await openFields(page); await page.getByRole('button', { name: '新增输出', exact: true }).click();
  await page.getByRole('tab', { name: '启动提示词', exact: true }).click();
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('尚未保存的执行步骤');
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect(page.getByRole('tab', { name: /输入与输出/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('输出名称 1', { exact: true })).toBeFocused();
  expect(state.writes).toHaveLength(0);
  await page.getByLabel('输出名称 1', { exact: true }).fill('交付报告');
  state.reject(true); await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('节点已被其他人修改');
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeEnabled();
  await expect(page.getByLabel('输出名称 1', { exact: true })).toHaveValue('交付报告');
  await page.getByRole('tab', { name: '启动提示词', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('尚未保存的执行步骤');
  state.reject(false); await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expectSaved(page);
  expect(state.writes).toHaveLength(2); expect(state.writes[1].row_version).toBe(1);
});

test('switching or creating nodes protects drafts and cancel keeps the current editor', async ({ page }) => {
  const second = { ...node, id: 'second-node', name: '第二个节点' };
  await setup(page, [node, second]); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('当前草稿不能被丢失');
  await openNode(page); await expect(page.getByRole('alertdialog')).toHaveCount(0);
  await openNode(page, second.name);
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('当前草稿不能被丢失');
  await page.getByRole('button', { name: '新建未分类节点', exact: true }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
  await openNode(page, second.name);
  await page.getByRole('alertdialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.getByRole('heading', { name: second.name, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('main navigation and reload protect the current draft', async ({ page }) => {
  await setup(page); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('保留的草稿名称');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('保留的草稿名称');
  await page.getByRole('button', { name: '重新加载当前页面数据', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('保留的草稿名称');
  await page.route('**/api/v1/capability-directory?*', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], total: 0, counts: {}, collection_total: 0, page: 1, page_size: 20 }) }));
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.getByRole('navigation', { name: '能力模块' })).toBeVisible();
  await page.getByRole('button', { name: '节点中心', exact: true }).click(); await openNode(page);
  await expect(page.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
});

test('in-flight saves cannot duplicate or leak into another selected node', async ({ page }) => {
  const second = { ...node, id: 'second-node', name: '第二个节点' };
  const state = await setup(page, [node, second]); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('正在保存的节点');
  state.hold();
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
  await page.keyboard.press('ControlOrMeta+s');
  await openNode(page, second.name);
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('正在保存的节点');
  expect(state.writes).toHaveLength(1);
  state.release();
  await expectSaved(page);
  await openNode(page, second.name);
  await expect(page.getByRole('heading', { name: second.name, exact: true })).toBeVisible();
});

for (const [width, height] of [[1440, 900], [1440, 700], [1024, 768], [1280, 620]]) {
  test(`workspace uses independent scrolling and fixed actions at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    const long = { ...node, description: node.description.repeat(20), executor: { ...node.executor!, startup_prompt: '# 任务执行\n\n' + '逐项核对输入，保留完整证据与执行记录。\n'.repeat(100) } };
    await setup(page, [long]); await openNode(page);
    const text = page.getByRole('textbox', { name: '启动触发提示词', exact: true });
    await text.hover(); await page.mouse.wheel(0, 500);
    await expect.poll(() => text.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    const previous = await text.evaluate(element => element.scrollTop);
    await page.getByRole('tab', { name: '上下文', exact: true }).click();
    await page.getByRole('tab', { name: '启动提示词', exact: true }).click();
    expect(await text.evaluate(element => element.scrollTop)).toBe(previous);
    await page.getByRole('button', { name: '预览', exact: true }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    expect(await text.evaluate(element => element.scrollTop)).toBe(previous);
    const bounds = await page.locator('.node-center-detail,.node-detail-editor').evaluateAll(elements => elements.map(element => ({ height: element.clientHeight, scroll: element.scrollHeight, width: element.clientWidth, scrollWidth: element.scrollWidth })));
    expect(bounds.every(box => box.scroll <= box.height + 1 && box.scrollWidth <= box.width + 1)).toBe(true);
    const actions = page.getByRole('button', { name: '保存节点', exact: true });
    await expect(actions).toBeInViewport();
    expect(await page.evaluate(() => ({ height: document.documentElement.scrollHeight, viewport: innerHeight, top: scrollY }))).toEqual({ height, viewport: height, top: 0 });
    await expect(text).toBeInViewport();
    await expect(page.locator('.node-fixed-metadata > p')).toHaveAttribute('title', long.description);
    await page.screenshot({ path: test.info().outputPath(`workspace-${width}-${height}.png`) });
  });
}

test('canceling settings navigation creates no temporary tab and confirmed navigation clears the guard', async ({ page }) => {
  await setup(page); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('未保存节点名称');
  await page.getByRole('button', { name: '账户与设置', exact: true }).click();
  await page.getByRole('menuitem', { name: '大模型配置', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.locator('.context-tab')).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('未保存节点名称');
  await page.getByRole('button', { name: '账户与设置', exact: true }).click();
  await page.getByRole('menuitem', { name: '大模型配置', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.locator('.context-tab.active')).toContainText('大模型配置');
  await page.getByRole('button', { name: '关闭大模型配置', exact: true }).click();
  await expect(page.getByRole('button', { name: '节点中心', exact: true })).toHaveClass('active');
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('enlarged prompt saves with the shortcut and exposes errors without losing text', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  await page.getByRole('button', { name: '放大编辑启动触发提示词' }).click();
  const expanded = page.getByRole('dialog');
  await expanded.getByRole('textbox').fill('放大编辑的内容');
  state.reject(true); await expanded.getByRole('textbox').press('ControlOrMeta+s');
  await expect(expanded.getByRole('alert')).toContainText('节点已被其他人修改');
  await expect(expanded.getByRole('textbox')).toHaveValue('放大编辑的内容');
  state.reject(false); await expanded.getByRole('textbox').press('ControlOrMeta+s');
  await expect(expanded.getByRole('alert')).toHaveCount(0);
  await expectSaved(page);
  expect(state.writes).toHaveLength(2);
  await expanded.getByRole('button', { name: '完成', exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(expanded.getByRole('button', { name: '编辑', exact: true })).toBeFocused();
  await expanded.screenshot({ path: test.info().outputPath('expanded-editor.png') });
  await expanded.getByRole('button', { name: '完成', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('放大编辑的内容');
});

test('many fields and the node directory scroll independently with fixed node controls', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  const fields = Array.from({ length: 20 }, (_, i) => ({ field_key: `field_${i}`, display_name: `输入资料 ${i}`, data_type: 'FILE' as const, description: `背景与验收证据 ${i}` }));
  const configured = { ...node, inputs: fields, outputs: [{ field_key: 'report', display_name: '交付报告', data_type: 'URL' as const, description: '报告与验证记录' }] };
  const records = [configured, ...Array.from({ length: 30 }, (_, index) => ({ ...node, id: `node-${index}`, name: `研发任务节点 ${index}` }))];
  await setup(page, records); await openNode(page); await openFields(page);
  const panel = page.getByRole('tabpanel');
  await panel.hover(); await page.mouse.wheel(0, 700);
  await expect.poll(() => panel.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  const fieldScroll = await panel.evaluate(element => element.scrollTop);
  await expect(page.getByRole('button', { name: '保存节点', exact: true })).toBeInViewport();
  await page.locator('.node-center-tree').hover(); await page.mouse.wheel(0, 600);
  await expect.poll(() => page.locator('.node-center-tree').evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  expect(await panel.evaluate(element => element.scrollTop)).toBe(fieldScroll);
  expect(await page.evaluate(() => scrollY)).toBe(0);
  await panel.evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: test.info().outputPath('fields-workspace.png') });
});


test('browser refresh warns about an unsaved draft and cancellation preserves it', async ({ page }) => {
  await setup(page); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('刷新前保留的草稿');
  const warning = page.waitForEvent('dialog');
  const reload = page.reload({ timeout: 2000 }).catch(error => { expect(String(error)).toMatch(/Timeout|ERR_ABORTED/); });
  const dialog = await warning;
  expect(dialog.type()).toBe('beforeunload'); await dialog.dismiss(); await reload;
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('刷新前保留的草稿');
});

test('deleting a different node keeps the selected node and its unsaved text', async ({ page }) => {
  const second = { ...node, id: 'second-node', name: '待删除节点' };
  await setup(page, [node, second]); await openNode(page);
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('当前节点的未保存提示词');
  await page.getByRole('button', { name: `删除节点 ${second.name}`, exact: true }).click();
  await expect(page.getByRole('alertdialog')).toContainText('删除节点');
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByTestId('node-card').filter({ hasText: second.name })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('当前节点的未保存提示词');
  await page.getByRole('button', { name: `删除节点 ${node.name}`, exact: true }).click();
  await expect(page.getByRole('alertdialog')).toContainText('放弃未保存');
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByTestId('node-card')).toHaveCount(1);
});


test('deleting the directory of an uncreated draft also protects its fields', async ({ page }) => {
  await setup(page, [], true);
  await page.getByRole('button', { name: '在 研发流程 新建节点', exact: true }).click();
  await openFields(page); await page.getByRole('button', { name: '新增输入', exact: true }).click();
  await page.getByLabel('输入名称 1', { exact: true }).fill('尚未保存的字段');
  await page.getByRole('button', { name: '删除目录 研发流程', exact: true }).click();
  await expect(page.getByRole('alertdialog')).toContainText('放弃未保存');
  await page.getByRole('alertdialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByLabel('输入名称 1', { exact: true })).toHaveValue('尚未保存的字段');
});


test('cancel restores prompts and all field changes while retaining the current node and tab', async ({ page }) => {
  const original = { ...node, inputs: [{ field_key: 'source', display_name: '来源', data_type: 'FILE' as const, description: '原始说明' }] };
  const state = await setup(page, [original]); await openNode(page);
  const cancel = page.getByRole('button', { name: '取消', exact: true });
  await expect(cancel).toBeDisabled();
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('未保存启动提示词');
  await page.getByRole('tab', { name: '上下文', exact: true }).click();
  await page.getByRole('textbox', { name: '上下文提示词', exact: true }).fill('未保存上下文');
  await openFields(page);
  await page.getByLabel('输入说明 1', { exact: true }).fill('未保存字段说明');
  await page.getByRole('button', { name: '新增输出', exact: true }).click();
  await page.getByLabel('输出名称 1', { exact: true }).fill('临时报告');
  await expect(cancel).toBeEnabled(); await cancel.click();
  await expect(cancel).toBeDisabled();
  await expect(page.getByRole('tab', { name: /输入与输出/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('输入说明 1', { exact: true })).toHaveValue('原始说明');
  await expect(page.getByLabel('输出名称 1', { exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: '上下文', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '上下文提示词', exact: true })).toHaveValue(node.executor!.context_prompt);
  await page.getByRole('tab', { name: '启动提示词', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue(node.executor!.startup_prompt);
  await expect(page.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
  expect(state.writes).toHaveLength(0);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('heading', { name: node.name, exact: true })).toBeVisible();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('Escape resets to the latest saved snapshot and respects input composition', async ({ page }) => {
  const state = await setup(page); await openNode(page);
  const text = page.getByRole('textbox', { name: '启动触发提示词', exact: true });
  await text.fill('最近保存的步骤'); await text.press('ControlOrMeta+s'); await expectSaved(page);
  await text.fill('第二轮未保存的步骤');
  await text.dispatchEvent('keydown', { key: 'Escape', isComposing: true, bubbles: true, cancelable: true });
  await expect(text).toHaveValue('第二轮未保存的步骤');
  await text.press('Escape');
  await expect(text).toHaveValue('最近保存的步骤'); await expectSaved(page);
  expect(state.writes).toHaveLength(1);
  await page.getByRole('button', { name: '放大编辑启动触发提示词', exact: true }).click();
  const expanded = page.getByRole('dialog');
  await expanded.getByRole('textbox').fill('放大窗口中未保存的内容');
  await page.keyboard.press('Escape');
  await expect(expanded).toHaveCount(0);
  await expect(text).toHaveValue('最近保存的步骤');
  await expectSaved(page); expect(state.writes).toHaveLength(1);
});

test('cancel clears a new draft without creating or leaving the editor', async ({ page }) => {
  const state = await setup(page, []);
  await page.getByRole('button', { name: '新建未分类节点', exact: true }).click();
  const cancel = page.getByRole('button', { name: '取消', exact: true });
  await expect(cancel).toBeDisabled();
  await page.getByLabel('节点名称', { exact: true }).fill('取消的新节点');
  await page.getByLabel('节点说明', { exact: true }).fill('取消的说明');
  await page.getByRole('textbox', { name: '启动触发提示词', exact: true }).fill('取消的步骤');
  await expect(cancel).toBeEnabled(); await cancel.click();
  await expect(page.getByLabel('节点名称', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('节点说明', { exact: true })).toHaveValue('');
  await expect(page.getByRole('textbox', { name: '启动触发提示词', exact: true })).toHaveValue('');
  await expect(cancel).toBeDisabled(); expect(state.writes).toHaveLength(0);
});

test('field descriptions use bounded single-line inputs while legacy contents survive an unrelated save', async ({ page }) => {
  const legacyDescription = '原有详细说明'.repeat(30);
  const configured = { ...node, inputs: [{ field_key: 'source', display_name: '来源', data_type: 'FILE' as const, description: legacyDescription }], outputs: [{ field_key: 'report', display_name: '报告', data_type: 'URL' as const, description: '' }] };
  const state = await setup(page, [configured]); await openNode(page);
  await page.screenshot({ path: test.info().outputPath('prompt-surface.png') });
  await openFields(page);
  const legacy = page.getByLabel('输入说明 1', { exact: true });
  const description = page.getByLabel('输出说明 1', { exact: true });
  expect(await description.evaluate(element => element.tagName)).toBe('INPUT');
  await expect(description).toHaveAttribute('maxlength', '120');
  await expect(legacy).toHaveValue(legacyDescription);
  await expect(legacy).toHaveAttribute('title', legacyDescription);
  await description.fill('说明'.repeat(60));
  await description.press('End'); await page.keyboard.insertText('超出限制');
  await expect(description).toHaveValue('说明'.repeat(60));
  await page.getByRole('group', { name: '输出类型 1', exact: true }).getByRole('button', { name: '文件', exact: true }).click();
  await page.screenshot({ path: test.info().outputPath('field-surface.png') });
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expectSaved(page);
  expect(state.writes[0].inputs[0].description).toBe(legacyDescription);
  expect(state.writes[0].outputs[0]).toMatchObject({ description: '说明'.repeat(60), data_type: 'FILE' });
  await page.getByRole('tab', { name: '上下文', exact: true }).click();
  await page.getByRole('textbox', { name: '上下文提示词', exact: true }).fill('更新约束');
  await page.getByRole('button', { name: '保存节点', exact: true }).click();
  await expectSaved(page);
  expect(state.writes[1].inputs[0].description).toBe(legacyDescription);
});
