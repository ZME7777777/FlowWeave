import { expect, test } from '@playwright/test';

test('wide model configuration lists twelve services and shows one selected detail', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const providers = Array.from({ length: 12 }, (_, index) => ({
    id: `provider-${index}`, name: `模型服务 ${index + 1}`, base_url: 'https://models.example.test/v1',
    auth_type: 'API_KEY', api_protocol: 'CHAT_COMPLETIONS', has_api_key: true, api_key_hint: '••••key',
    oauth_connected: false, oauth_device_pending: false, connection_state: 'CONNECTED',
    reference_node_count: 0, available_for_nodes: true, available_for_prompt_gates: true, row_version: 1,
    models: [{ id: `model-${index}`, model_name: 'gpt-test', enabled: true, is_default: true }],
    created_at: '2026-08-31T00:00:00Z', updated_at: '2026-08-31T00:00:00Z',
  }));
  await page.route('**/api/v1/auth/me', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'model-manager', username: 'manager', role: 'USER' }) }));
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(providers) }));
  await page.goto('/');
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '大模型配置', exact: true }).click();
  await expect(page.locator('.settings-resource-row')).toHaveCount(12);
  await expect(page.locator('.settings-model-detail')).toHaveCount(1);
  await expect(page.getByRole('heading', { name: '模型服务 1', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '列表分页' })).toHaveCount(0);
});
