import { expect, test } from '@playwright/test';

const apiBase = process.env.E2E_API_URL ?? 'http://127.0.0.1:8080';

test('Tool Policy is configured visually and published as an immutable version', async ({ page, request }) => {
  const policyName = `ui-tool-policy-${Date.now().toString(36)}`;
  let publishedVersionId: string | undefined;

  try {
    await page.goto('/');
    await page.getByRole('button', { name: '能力仓库' }).click();
    await page.getByRole('button', { name: /Tool Policy/ }).click();
    await page.getByRole('button', { name: '新建 Tool Policy' }).click();

    const dialog = page.getByRole('dialog', { name: '新建 Tool Policy' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('heading', { name: '配置并发布 Tool Policy' })).toBeVisible();
    await expect(dialog.getByRole('button', { name: '可视化配置' })).toHaveClass(/active/);
    await expect(dialog).toContainText('从 15 项当前目录中选择策略允许使用的工具');
    await expect(dialog.locator('.tool-policy-tool-grid > article')).toHaveCount(15);
    await expect(dialog.getByText('当前选择')).toHaveCount(0);

    const browserTool = dialog.locator('.tool-policy-tool-grid > article').filter({ hasText: 'browser_tool_set' });
    await expect(browserTool.getByRole('checkbox')).toBeDisabled();
    await expect(browserTool).toContainText('尚未安装网络、凭据、产物与 SSRF 安全控制');
    await dialog.getByRole('button', { name: '全选', exact: true }).click();
    const availableTools = dialog.locator('.tool-policy-tool-grid input[type="checkbox"]:not(:disabled)');
    const availableToolCount = await availableTools.count();
    expect(availableToolCount).toBeGreaterThan(0);
    for (let index = 0; index < availableToolCount; index += 1) {
      await expect(availableTools.nth(index)).toBeChecked();
    }
    await expect(dialog.getByRole('button', { name: '取消全选', exact: true })).toBeVisible();

    const terminalTool = dialog.locator('.tool-policy-tool-grid > article').filter({ hasText: 'terminal' });
    await expect(terminalTool.getByRole('checkbox')).toBeChecked();
    await terminalTool.getByRole('combobox').selectOption('subprocess');
    await dialog.getByLabel('策略名称').fill(policyName);
    await dialog.getByLabel('策略说明').fill('通过可视化工具目录创建的 E2E 策略');

    await dialog.getByRole('button', { name: 'JSON 预览' }).click();
    const preview = dialog.locator('.tool-policy-json-preview pre');
    await expect(preview).toContainText(policyName);
    await expect(preview).toContainText('"terminal_type": "subprocess"');
    await expect(preview).toContainText('"task_tracker"');
    await page.screenshot({ path: '/tmp/flowweave-tool-policy-editor.png', fullPage: true });

    await dialog.getByRole('button', { name: '可视化配置' }).click();
    const validation = page.waitForResponse(response => response.url().endsWith('/api/v1/capability-imports/validate') && response.request().method() === 'POST');
    const commit = page.waitForResponse(response => response.url().endsWith('/api/v1/capability-imports') && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: /保存并发布/ }).click();
    expect((await validation).ok()).toBeTruthy();
    expect((await commit).ok()).toBeTruthy();
    await expect(dialog).toBeHidden();
    await expect(page.getByRole('status')).toContainText(`已发布 Tool Policy“${policyName}”的不可变版本`);
    await expect(page.locator('.capability-card').filter({ hasText: policyName })).toBeVisible();

    const capabilities = await request.get(`${apiBase}/api/v1/capabilities`);
    expect(capabilities.ok(), await capabilities.text()).toBeTruthy();
    const versions = await capabilities.json() as Array<{ id: string; capability_key: string }>;
    publishedVersionId = versions.find(item => item.capability_key === policyName)?.id;
    expect(publishedVersionId).toBeTruthy();
  } finally {
    if (publishedVersionId) {
      const removed = await request.delete(`${apiBase}/api/v1/capabilities/${publishedVersionId}`);
      expect(removed.ok(), await removed.text()).toBeTruthy();
    }
  }
});
