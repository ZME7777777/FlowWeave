import { expect, test } from '@playwright/test';

test('Context module opens a titled text upload form', async ({ page }) => {
  await page.route('**/api/v1/capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' })
    .getByRole('button', { name: /Context/ }).click();

  await page.getByRole('button', { name: '新增 Context' }).click();
  const dialog = page.getByRole('dialog', { name: '新增 Context' });
  await expect(dialog.getByLabel('Context 标题')).toBeVisible();
  await expect(dialog.getByLabel('Context 说明')).toBeVisible();
  await expect(dialog.getByLabel('Context 文件')).toHaveAttribute(
    'accept',
    '.txt,.md,.markdown,.zip,text/plain,text/markdown,application/zip',
  );
});

test('Context Bundle is parsed before its editable directory is published', async ({ page }) => {
  let validateCount = 0;
  let confirmedManifest: Record<string, unknown> | undefined;
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capability-imports/validate', async route => {
    validateCount += 1;
    const manifest = route.request().postDataJSON().context_bundle_manifest as Record<string, unknown> | undefined;
    if (validateCount === 2) confirmedManifest = manifest;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        import_token: `token-${validateCount}`, content_hash: 'a'.repeat(64),
        preview: { capabilities: [{ capability_key: '资料包', normalized_config: { manifest: {
          entrypoint: 'README.md', conflict_policy: 'ORDERED_DOCUMENTS_LATER_WINS', documents: [
            { path: 'README.md', title: '总览', content_sha256: '1'.repeat(64) },
            { path: 'guides/02.md', title: '第二章', content_sha256: '2'.repeat(64) },
          ],
        } } }] },
      }),
    });
  });
  await page.route('**/api/v1/capability-imports', route => route.fulfill({
    status: 201, contentType: 'application/json', body: JSON.stringify({ capabilities: [{ capability_key: '资料包' }] }),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
  await page.getByRole('button', { name: '新增 Context' }).click();
  const dialog = page.getByRole('dialog', { name: '新增 Context' });
  await dialog.getByLabel('Context 标题').fill('资料包');
  await dialog.getByLabel('Context 文件').setInputFiles({ name: 'bundle.zip', mimeType: 'application/zip', buffer: Buffer.from('zip') });
  await dialog.getByRole('button', { name: '解析资料包' }).click();
  await expect(dialog.getByText('资料包目录')).toBeVisible();
  await dialog.getByLabel('资料标题 guides/02.md').fill('最终章节');
  await dialog.getByRole('button', { name: '上移 guides/02.md' }).click();
  await dialog.getByRole('button', { name: '资料包入口' }).click();
  await dialog.getByRole('option', { name: 'guides/02.md' }).click();
  await dialog.getByRole('button', { name: '确认并发布' }).click();
  await expect(page.getByRole('status')).toContainText('已发布 Context“资料包”');
  expect(confirmedManifest).toEqual({
    entrypoint: 'guides/02.md', conflict_policy: 'ORDERED_DOCUMENTS_LATER_WINS', documents: [
      { path: 'guides/02.md', title: '最终章节' }, { path: 'README.md', title: '总览' },
    ],
  });
});

test('published Context Bundle shows its frozen document directory before compiled text', async ({ page }) => {
  const bundle = {
    id: 'bundle-version', lineage_id: 'bundle-lineage', revision_number: 1, is_latest: true,
    capability_type: 'CONTEXT', capability_key: '资料包', description: '关联背景资料', version: '',
    filename: 'bundle.zip', content_hash: 'b'.repeat(64), byte_size: 1024, import_id: 'bundle-import',
    created_at: '2026-09-02T00:00:00Z', reference_count: 0, is_builtin: false, document: {},
    dependencies: {}, dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null,
  };
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([bundle]) }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capabilities/bundle-version/context-source', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      id: bundle.id, capability_key: bundle.capability_key, filename: bundle.filename,
      description: bundle.description, content_format: 'BUNDLE',
      manifest: {
        entrypoint: 'README.md', conflict_policy: 'ORDERED_DOCUMENTS_LATER_WINS', documents: [
          { path: 'README.md', title: '总览', content_sha256: '1'.repeat(64) },
          { path: 'guides/02.md', title: '最终说明', content_sha256: '2'.repeat(64) },
        ],
      },
      content: '[资料包 · 关联资料包]\n\n## 文档：总览',
    }),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
  await page.getByRole('button', { name: '查看内容' }).click();
  const dialog = page.getByRole('dialog', { name: '查看 Context 资料包' });
  await expect(dialog.getByText('资料目录')).toBeVisible();
  await expect(dialog.getByText('入口：README.md · 全部文档均已加载')).toBeVisible();
  await expect(dialog.getByText('最终说明')).toBeVisible();
  await expect(dialog.locator('pre')).toContainText('## 文档：总览');
});

test('capability repository exposes module-specific menus and actions', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();

  const modules = page.getByRole('navigation', { name: '能力模块' });
  const actions = page.locator('.capability-list-controls');
  await expect(modules.getByRole('button')).toHaveCount(6);
  await expect(modules.getByRole('button', { name: /Skill/ })).toHaveAttribute('aria-current', 'page');
  await expect(actions.getByText('上传 Skill ZIP', { exact: true })).toBeVisible();
  await expect(actions.getByText('上传 Plugin ZIP', { exact: true })).toHaveCount(0);
  await expect(page.locator('.skill-collection-section')).toBeVisible();
  const collectionEditor = page.locator('form.capability-collection-editor');
  const collectionDialog = collectionEditor.getByRole('heading', { name: '新建 Skill 组合' });
  // The header action is always present, including when the local fixture has
  // no collections, so it is the stable entry point for this test.
  await page.getByRole('button', { name: '新建 Skill 组合' }).click();
  await expect(collectionDialog).toBeVisible();
  // The persistent local E2E database may already contain immutable Skill
  // versions from a preceding product scenario.  Both states must keep the
  // collection editor available.
  await expect(collectionEditor.getByText('还没有可选的 Skill 版本，请先关闭窗口并上传 Skill ZIP。')
    .or(collectionEditor.locator('.capability-collection-members input[type="checkbox"]').first())).toBeVisible();
  const closeEditor = collectionEditor.getByRole('button', { name: '关闭' });
  if (await closeEditor.count()) await closeEditor.click();
  else await collectionEditor.getByRole('button', { name: '取消' }).click();
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
    ['Agent Definition', '新建 Agent Definition'],
  ] as const;
  for (const [module, action] of moduleActions) {
    await modules.getByRole('button', { name: new RegExp(module) }).click();
    await expect(actions.getByRole('button')).toHaveCount(1);
    await expect(actions.getByRole('button', { name: action })).toBeVisible();
  }

  await modules.getByRole('button', { name: /Context/ }).click();
  await expect(actions.getByRole('button', { name: '新增 Context' })).toBeVisible();

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

test('Skill collections use a compact paged grid', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 480 });
  const collections = ['需求分析', '研发交付', '质量验证', '发布运营'].map((name, index) => ({
    id: `collection-${index + 1}`,
    name,
    category: '研发',
    description: `${name}组合说明`,
    row_version: 1,
    members: [],
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
  }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(collections),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  const collectionSection = page.locator('.skill-collection-section');
  const collectionList = collectionSection.locator('.capability-collection-list');
  const collectionRows = collectionList.locator(':scope > article');
  await expect(collectionRows).toHaveCount(3);
  const widths = await collectionRows.first().evaluate(row => {
    const list = row.parentElement;
    return { row: row.getBoundingClientRect().width, list: list?.getBoundingClientRect().width ?? 0 };
  });
  expect(widths.row).toBeLessThan(widths.list);
  const bounds = await collectionSection.evaluate(section => {
    const pager = section.querySelector('.skill-collection-pagination');
    const sectionBounds = section.getBoundingClientRect();
    const pagerBounds = pager?.getBoundingClientRect();
    return { sectionBottom: sectionBounds.bottom, pagerBottom: pagerBounds?.bottom ?? 0 };
  });
  expect(bounds.pagerBottom).toBeLessThanOrEqual(bounds.sectionBottom);
  await expect(collectionSection.getByText('需求分析', { exact: true })).toBeVisible();
  await expect(collectionSection.getByText('研发交付', { exact: true })).toBeVisible();
  await expect(collectionSection.getByText('质量验证', { exact: true })).toBeVisible();
  await expect(collectionSection.getByText('发布运营', { exact: true })).toHaveCount(0);
  await collectionSection.getByRole('button', { name: '下一页' }).click();
  await expect(collectionRows).toHaveCount(1);
  await expect(collectionSection.getByText('发布运营', { exact: true })).toBeVisible();
  await expect(collectionSection.getByRole('button', { name: '上一页' })).toBeEnabled();
});

test('every capability module supports selecting and bulk deleting capability lineages', async ({ page }) => {
  const moduleTypes = ['SKILL', 'PLUGIN', 'MCP', 'HOOK', 'AGENT_DEFINITION', 'CONTEXT'] as const;
  const labels: Record<(typeof moduleTypes)[number], string> = {
    SKILL: 'Skill',
    PLUGIN: 'Plugin',
    MCP: 'MCP',
    HOOK: 'Hook',
    AGENT_DEFINITION: 'Agent Definition',
    CONTEXT: 'Context',
  };
  let capabilities = moduleTypes.flatMap((capabilityType, moduleIndex) => [0, 1].map(itemIndex => ({
    id: `version-${moduleIndex}-${itemIndex}`,
    lineage_id: `lineage-${moduleIndex}-${itemIndex}`,
    revision_number: 1,
    is_latest: true,
    capability_type: capabilityType,
    capability_key: `bulk-delete-${capabilityType.toLowerCase()}-${itemIndex}`,
    description: `${labels[capabilityType]} batch delete fixture`,
    version: '1.0.0',
    filename: `${capabilityType.toLowerCase()}.json`,
    content_hash: `${moduleIndex}${itemIndex}`.padStart(64, '0'),
    byte_size: 128,
    import_id: `import-${moduleIndex}-${itemIndex}`,
    created_at: new Date(2026, 7, 24, 10, moduleIndex, itemIndex).toISOString(),
    reference_count: 0,
    is_builtin: false,
    document: {},
    dependencies: {},
    dependency_build_state: 'NOT_REQUIRED',
    dependency_build_error: null,
  })));
  capabilities.push({
    ...capabilities.find(item => item.capability_type === 'CONTEXT')!,
    id: 'version-builtin-context',
    lineage_id: 'lineage-builtin-context',
    capability_key: 'builtin-default-context',
    is_builtin: true,
  });

  await page.route('**/api/v1/capabilities', async route => {
    if (route.request().method() === 'DELETE') {
      const payload = route.request().postDataJSON() as { ids: string[] };
      capabilities = capabilities.filter(item => !payload.ids.includes(item.id));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          deleted_ids: payload.ids,
          blocked: [],
          collection_changes: { updated: [], deleted: [] },
        }),
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(capabilities) });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  const modules = page.getByRole('navigation', { name: '能力模块' });

  for (const capabilityType of moduleTypes) {
    const label = labels[capabilityType];
    await modules.getByRole('button', { name: new RegExp(label) }).click();
    const bulkDelete = page.getByRole('button', { name: '批量删除 (0)' });
    await expect(bulkDelete).toBeDisabled();
    const checkboxes = page.getByRole('checkbox', { name: new RegExp(`选择能力 bulk-delete-${capabilityType.toLowerCase()}-`) });
    await expect(checkboxes).toHaveCount(2);
    if (capabilityType === 'CONTEXT') await expect(page.getByRole('checkbox', { name: '选择能力 builtin-default-context' })).toBeDisabled();
    await page.getByRole('button', { name: '全选当前模块' }).click();
    await expect(checkboxes.nth(0)).toBeChecked();
    await expect(checkboxes.nth(1)).toBeChecked();
    await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
    await page.getByRole('button', { name: '批量删除 (2)' }).click();
    const confirm = page.getByRole('alertdialog');
    await expect(confirm).toContainText('删除所选的 2 项能力');
    await expect(confirm).toContainText('全部历史版本');
    await confirm.getByRole('button', { name: '确认删除' }).click();
    await expect(checkboxes).toHaveCount(0);
    await expect(page.getByRole('status')).toContainText('已删除 2 条无关联能力记录');
  }
});
