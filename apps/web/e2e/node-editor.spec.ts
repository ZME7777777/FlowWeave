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

test('node prompt keeps free text and selected Context versions together', async ({ page }) => {
  await page.route('**/api/v1/node-directories', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/node-assets**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([node]),
  }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{
      id: 'context-version-1',
      lineage_id: 'context-lineage-1',
      revision_number: 1,
      is_latest: true,
      capability_type: 'CONTEXT',
      capability_key: '产品背景',
      description: '固定背景',
      filename: 'product.md',
      content_hash: 'a'.repeat(64),
      byte_size: 16,
      created_at: '2026-08-31T00:00:00Z',
      reference_count: 0,
      is_builtin: false,
      dependencies: {},
      dependency_build_state: 'NOT_REQUIRED',
      dependency_build_error: null,
    }]),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByTestId('node-card').filter({ hasText: node.name }).getByTitle('编辑').click();
  await page.getByRole('button', { name: /提示词/ }).click();

  const contextPrompt = page.getByLabel('上下文提示词');
  const contextSelect = page.getByLabel('Context 能力');
  await contextPrompt.fill('同时保留这段自由上下文');
  await contextSelect.selectOption('context-version-1');

  await expect(contextPrompt).toHaveValue('同时保留这段自由上下文');
  await expect(contextSelect.locator('option:checked')).toHaveAttribute('value', 'context-version-1');
  await expect(page.getByText('不作为普通用户消息发送。')).toBeVisible();
});
