import { expect, test } from '@playwright/test';

test('capability repository exposes module-specific menus and actions', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();

  const modules = page.getByRole('navigation', { name: '能力模块' });
  const actions = page.locator('.capability-module-actions');
  await expect(modules.getByRole('button')).toHaveCount(6);
  await expect(modules.getByRole('button', { name: /Skill/ })).toHaveAttribute('aria-current', 'page');
  await expect(actions.getByText('上传 Skill ZIP', { exact: true })).toBeVisible();
  await expect(actions.getByText('上传 Plugin ZIP', { exact: true })).toHaveCount(0);
  await expect(page.locator('.skill-collection-section')).toBeVisible();
  const collectionEditor = page.locator('form.capability-collection-editor');
  const collectionDialog = collectionEditor.getByRole('heading', { name: '新建 Skill 组合' });
  await page.getByRole('button', { name: '新建 Skill 组合' }).click();
  await expect(collectionDialog).toBeVisible();
  // The persistent local E2E database may already contain immutable Skill
  // versions from a preceding product scenario.  Both states must keep the
  // collection editor available.
  await expect(collectionEditor.getByText('还没有可选的 Skill 版本，请先关闭窗口并上传 Skill ZIP。')
    .or(collectionEditor.locator('.capability-collection-members input[type="checkbox"]').first())).toBeVisible();
  await collectionEditor.getByRole('button', { name: '关闭' }).click();
  await expect(collectionDialog).toBeHidden();
  await page.getByRole('button', { name: '创建第一个 Skill 组合' }).click();
  await expect(collectionDialog).toBeVisible();
  await collectionEditor.getByRole('button', { name: '取消' }).click();
  await expect(collectionDialog).toBeHidden();

  await modules.getByRole('button', { name: /Plugin/ }).click();
  await expect(actions.getByText('上传 Plugin ZIP', { exact: true })).toBeVisible();
  await expect(actions.getByRole('button', { name: '浏览 Marketplace' })).toBeVisible();
  await expect(actions.getByRole('button', { name: 'Git Plugin' })).toBeVisible();
  await expect(actions.getByText('上传 Skill ZIP', { exact: true })).toHaveCount(0);
  await expect(page.locator('.skill-collection-section')).toHaveCount(0);

  const moduleActions = [
    ['MCP', '新建 MCP'],
    ['Hook', '新建 Hook'],
    ['Tool Policy', '新建 Tool Policy'],
    ['Agent Definition', '新建 Agent Definition'],
  ] as const;
  for (const [module, action] of moduleActions) {
    await modules.getByRole('button', { name: new RegExp(module) }).click();
    await expect(actions.getByRole('button')).toHaveCount(1);
    await expect(actions.getByRole('button', { name: action })).toBeVisible();
  }

  await modules.getByRole('button', { name: /MCP/ }).click();
  await actions.getByRole('button', { name: '新建 MCP' }).click();
  const mcpDialog = page.getByRole('dialog', { name: '新建 MCP' });
  const connectionTypes = mcpDialog.getByRole('complementary', { name: 'MCP 连接类型' });
  await expect(connectionTypes.getByRole('button')).toHaveCount(2);
  await expect(connectionTypes.getByRole('button', { name: /远程/ })).toBeVisible();
  await expect(connectionTypes.getByRole('button', { name: /本地/ })).toBeVisible();
  await expect(connectionTypes.getByText('docs', { exact: true })).toHaveCount(0);
  await expect(connectionTypes.getByText('localTools', { exact: true })).toHaveCount(0);
  await expect(mcpDialog.getByLabel('远程协议')).toBeVisible();
  await expect(mcpDialog.getByLabel('Server URL')).toBeVisible();
  await expect(mcpDialog.getByLabel('CLI 命令')).toHaveCount(0);

  await connectionTypes.getByRole('button', { name: /本地/ }).click();
  await expect(mcpDialog.getByLabel('连接协议')).toHaveValue('stdio');
  await expect(mcpDialog.getByLabel('CLI 命令')).toBeVisible();
  await expect(mcpDialog.getByLabel('Server URL')).toHaveCount(0);
  await expect(mcpDialog.getByLabel('远程协议')).toHaveCount(0);
  await mcpDialog.getByRole('button', { name: '关闭' }).click();

  await expect(page.locator('.capability-import-actions')).toHaveCount(0);
  await expect(page.locator('.git-plugin-entry')).toHaveCount(0);
  await expect(page.locator('.capability-guidance')).toHaveCount(0);
  await expect(page.locator('.capability-tools')).toHaveCount(0);
});
