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
  await expect(page.getByLabel('启动触发提示词')).toBeVisible();
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

test('node prompt stays editable in the compact single-page editor', async ({ page }) => {
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([node]),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点中心', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).click();

  const contextPrompt = page.getByLabel('上下文提示词');
  await contextPrompt.fill('同时保留这段自由上下文');

  await expect(contextPrompt).toHaveValue('同时保留这段自由上下文');
  await expect(page.getByRole('button', { name: '保存节点' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '输入输出定义' })).toHaveCount(0);
  const detailGeometry = await page.locator('.node-center-detail').evaluate(detail => ({
    scrollWidth: detail.scrollWidth,
    clientWidth: detail.clientWidth,
  }));
  expect(detailGeometry.scrollWidth).toBe(detailGeometry.clientWidth);
});
