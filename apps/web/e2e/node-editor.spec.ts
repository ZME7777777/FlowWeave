import { expect, test } from '@playwright/test';

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

test('clicking a node card opens the editor directly', async ({ page }) => {
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([node]),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).click();

  await expect(page.getByRole('heading', { name: '编辑节点资产' })).toBeVisible();
  await expect(page.getByRole('dialog', { name: `节点详情 ${node.name}` })).toHaveCount(0);
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
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).getByTitle('编辑').click();

  await expect(page.getByLabel('节点名称')).toBeVisible();
  await expect(page.getByLabel('启动触发提示词')).toBeVisible();
  await expect(page.getByRole('heading', { name: '输入输出定义' })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存节点资产' })).toBeVisible();
  expect(updateCount).toBe(0);
});

test('node prompt stays editable in the single-page editor', async ({ page }) => {
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([node]),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).getByTitle('编辑').click();

  const contextPrompt = page.getByLabel('上下文提示词');
  await contextPrompt.fill('同时保留这段自由上下文');

  await expect(contextPrompt).toHaveValue('同时保留这段自由上下文');
  await expect(page.getByText('提示词在运行时注入，不作为普通用户消息发送。')).toBeVisible();
});
