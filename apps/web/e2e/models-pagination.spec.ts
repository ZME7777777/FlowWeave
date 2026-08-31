import { expect, test } from '@playwright/test';

test('wide model configuration view keeps twelve cards on its first page', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const providers = Array.from({ length: 12 }, (_, index) => ({
    id: `provider-${index}`,
    name: `模型服务 ${index + 1}`,
    base_url: 'https://models.example.test/v1',
    auth_type: 'API_KEY',
    has_api_key: true,
    api_key_hint: '••••key',
    oauth_connected: false,
    oauth_device_pending: false,
    connection_state: 'CONNECTED',
    reference_node_count: 0,
    available_for_nodes: true,
    available_for_prompt_gates: true,
    row_version: 1,
    models: [{ id: `model-${index}`, model_name: 'gpt-test', enabled: true, is_default: true }],
    created_at: '2026-08-31T00:00:00Z',
    updated_at: '2026-08-31T00:00:00Z',
  }));
  await page.route('**/api/v1/model-providers', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(providers),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '大模型配置', exact: true }).click();

  await expect(page.locator('.compact-model-list .model-config-card')).toHaveCount(12);
  await expect(page.getByRole('navigation', { name: '列表分页' })).toHaveCount(0);
});
