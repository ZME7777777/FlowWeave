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
  executor: { startup_prompt: '启动提示词', context_prompt: '上下文提示词' },
  created_at: '2026-08-30T00:00:00Z',
  updated_at: '2026-08-30T00:00:00Z',
};

test('next opens input/output step without saving or closing', async ({ page }) => {
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
  await page.getByRole('button', { name: /提示词/ }).click();
  await expect(page.getByLabel('启动触发提示词')).toBeVisible();

  await page.getByRole('button', { name: '下一步', exact: true }).click();

  await expect(page.getByRole('heading', { name: '编辑节点资产' })).toBeVisible();
  await expect(page.getByText('定义输入输出', { exact: true })).toBeVisible();
  expect(updateCount).toBe(0);
});
