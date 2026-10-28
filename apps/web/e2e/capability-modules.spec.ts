import { expect, test, type Page, type Route } from '@playwright/test';


// Legacy fixtures remain shared with editor/download/bulk-delete requests;
// expose the new server page contract without letting the UI see the full list.
async function mockRepository(page: Page, kind: 'capabilities' | 'capability-collections', handler: (route: Route) => Promise<unknown>) {
  await page.route(`**/api/v1/${kind}`, handler);
  const path = kind === 'capabilities' ? 'capability-directory' : 'capability-collection-directory';
  await page.route(`**/api/v1/${path}?*`, async route => {
    const adapted = new Proxy(route, { get(target, key) {
      if (key === 'fulfill') return async (response: { body?: string; status?: number }) => {
        const records = JSON.parse(response.body ?? '[]') as Array<Record<string, unknown>>;
        const params = new URL(route.request().url()).searchParams;
        let items: Array<Record<string, unknown>> = records;
        const counts: Record<string, number> = {};
        if (kind === 'capabilities') {
          const groups = new Map<string, Array<Record<string, unknown>>>();
          records.forEach(item => groups.set(String(item.lineage_id), [...(groups.get(String(item.lineage_id)) ?? []), item]));
          items = [...groups].map(([id, versions]) => ({ id, latest: versions.find(item => item.is_latest) ?? versions[0], versions }));
          items.forEach(item => { const type = String((item.latest as Record<string, unknown>).capability_type); counts[type] = (counts[type] ?? 0) + 1; });
          items = items.filter(item => (item.latest as Record<string, unknown>).capability_type === params.get('capability_type'));
        }
        const value = (item: Record<string, unknown>, column: string) => {
          const asset = (kind === 'capabilities' ? item.latest : item) as Record<string, unknown>;
          return column === 'name' ? String(asset.capability_key ?? asset.name) : column === 'member_count' ? (asset.members as unknown[]).length : asset[column] as string | number;
        };
        const needle = (params.get('q') ?? '').toLowerCase();
        items = items.filter(item => !needle || JSON.stringify(item).toLowerCase().includes(needle));
        const sort = params.get('sort') ?? 'created_at'; const sign = params.get('direction') === 'asc' ? 1 : -1;
        items.sort((a, b) => { const left = value(a, sort); const right = value(b, sort); return left < right ? -sign : left > right ? sign : String(a.id).localeCompare(String(b.id)); });
        const number = Number(params.get('page') ?? 1); const size = Number(params.get('page_size') ?? 20);
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: items.slice((number - 1) * size, number * size), total: items.length, page: number, page_size: size, counts, collection_total: 0 }) });
      };
      const value = Reflect.get(target, key); return typeof value === 'function' ? value.bind(target) : value;
    } });
    await handler(adapted);
  });
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'capability-user', username: 'capability-user', role: 'USER', is_super_admin: false }),
  }));
});

test('Context module supports direct text, uploads, and ZIP bundles', async ({ page }) => {
  await mockRepository(page, 'capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({
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
  await expect(dialog.getByLabel('Context 内容')).toBeVisible();
  await expect(dialog.getByRole('button', { name: '上传文件' })).toBeVisible();
  await expect(dialog.getByText('直接输入或粘贴文本，也可拖入文件或 ZIP 资料包。')).toBeVisible();
  await expect(dialog.getByLabel('Context 文件')).toHaveAttribute(
    'accept',
    '.txt,.md,.markdown,.zip,text/plain,text/markdown,application/zip',
  );
});

test('Context direct text is published through the existing validation flow', async ({ page }) => {
  let validationBody: Record<string, unknown> | undefined;
  await mockRepository(page, 'capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capability-imports/validate', async route => {
    validationBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ import_token: 'text-token', content_hash: 'a'.repeat(64), preview: { capabilities: [{ capability_key: '直接编写 Context' }] } }),
    });
  });
  await page.route('**/api/v1/capability-imports', route => route.fulfill({
    status: 201, contentType: 'application/json', body: JSON.stringify({ capabilities: [{ capability_key: '直接编写 Context' }] }),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
  await page.getByRole('button', { name: '新增 Context' }).click();
  const dialog = page.getByRole('dialog', { name: '新增 Context' });
  await dialog.getByLabel('Context 标题').fill('直接编写 Context');
  await dialog.getByLabel('Context 内容').fill('可直接粘贴的背景信息');
  await expect(dialog.getByText('将作为 context.txt 发布')).toBeVisible();
  await dialog.getByRole('button', { name: '校验并发布' }).click();
  await expect(page.getByRole('status')).toContainText('已发布 Context“直接编写 Context”');

  expect(validationBody).toMatchObject({
    capability_type: 'CONTEXT', filename: 'context.txt', context_title: '直接编写 Context',
  });
  expect(Buffer.from(String(validationBody?.content_base64), 'base64').toString()).toBe('可直接粘贴的背景信息');
});

test('Context Bundle is parsed before its editable directory is published', async ({ page }) => {
  let validateCount = 0;
  let confirmedManifest: Record<string, unknown> | undefined;
  await mockRepository(page, 'capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
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
  await expect(page.locator('.capability-notice-layer')).toHaveCSS('height', '0px');
  await page.getByRole('button', { name: '关闭成功提示' }).click();
  await expect(page.getByRole('status')).toHaveCount(0);
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
  await mockRepository(page, 'capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([bundle]) }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capabilities/bundle-version/files', route => route.fulfill({
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
      compiled_content: '[资料包 · 关联资料包]\n\n## 文档：总览',
      files: [{ path: 'README.md', title: '总览', byte_size: 20 }, { path: 'guides/02.md', title: '最终说明', byte_size: 20 }],
    }),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
  const contextCard = page.getByRole('button', { name: '查看 Context 资料包' });
  await contextCard.click();
  await expect(page.getByRole('checkbox', { name: '选择能力 资料包', exact: true })).not.toBeChecked();
  const dialog = page.getByRole('dialog', { name: '查看 Context 资料包' });
  await expect(dialog.getByRole('treeitem', { name: '总内容', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(dialog.getByRole('treeitem', { name: 'README.md', exact: true })).toBeVisible();
  await expect(dialog.getByRole('treeitem', { name: '02.md', exact: true })).toBeVisible();
  await expect(dialog.locator('pre')).toContainText('## 文档：总览');
});

test('capability repository exposes module-specific menus and actions', async ({ page }) => {
  await mockRepository(page, 'capabilities', route => route.fulfill({ body: '[]' }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ body: '[]' }));
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();

  const modules = page.getByRole('navigation', { name: '能力模块' });
  const actions = page.locator('.capability-list-controls');
  await expect(modules.getByRole('button')).toHaveCount(7);
  await expect(modules.getByRole('button', { name: /^Skill\s+\d/ })).toHaveAttribute('aria-current', 'page');
  await expect(actions.getByRole('button', { name: '导入 Skill', exact: true })).toBeVisible();
  await expect(actions.getByText('上传 Plugin ZIP', { exact: true })).toHaveCount(0);
  await modules.getByRole('button', { name: /Skill 组合/ }).click();
  const collectionEditor = page.locator('form.capability-collection-editor');
  const collectionDialog = collectionEditor.getByRole('heading', { name: '新建 Skill 组合' });
  // The header action is always present, including when the local fixture has
  // no collections, so it is the stable entry point for this test.
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Skill 组合/ }).click();
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
  await expect(actions.getByRole('button', { name: '导入 Plugin' })).toBeVisible();
  await expect(actions.getByRole('button', { name: '市场源管理', exact: true })).toBeVisible();
  await expect(actions.getByRole('button')).toHaveCount(2);
  await expect(actions.getByRole('button', { name: '导入 Skill', exact: true })).toHaveCount(0);
  await expect(page.locator('.skill-collection-section')).toHaveCount(0);

  const moduleActions = [
    ['MCP', '新建 MCP'],
    ['Hook', '新建 Hook'],
  ] as const;
  for (const [module, action] of moduleActions) {
    await modules.getByRole('button', { name: new RegExp(module) }).click();
    await expect(actions.getByRole('button')).toHaveCount(1);
    await expect(actions.getByRole('button', { name: action })).toBeVisible();
  }

  await modules.getByRole('button', { name: /Agent Definition/ }).click();
  await expect(actions.locator('input[type="file"]')).toHaveCount(1);
  await expect(actions.getByText('上传 Agent Definition Markdown', { exact: true })).toBeVisible();

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

test('capability editors overlay the top navigation without clipping their headings', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const capabilities = [
    {
      id: 'skill-version', lineage_id: 'skill-lineage', revision_number: 1, is_latest: true,
      capability_type: 'SKILL', capability_key: 'layout-skill', description: 'Skill editor layout fixture',
      version: '1.0.0', filename: 'SKILL.md', content_hash: '1'.repeat(64), byte_size: 128,
      import_id: 'skill-import', created_at: '2026-09-02T00:00:00Z', reference_count: 0,
      is_builtin: false, document: {}, dependencies: {}, dependency_build_state: 'NOT_REQUIRED',
      dependency_build_error: null,
    },
    {
      id: 'mcp-version', lineage_id: 'mcp-lineage', revision_number: 1, is_latest: true,
      capability_type: 'MCP', capability_key: 'layout-mcp', description: 'MCP editor layout fixture',
      version: '1.0.0', filename: 'mcp.json', content_hash: '2'.repeat(64), byte_size: 128,
      import_id: 'mcp-import', created_at: '2026-09-02T00:00:00Z', reference_count: 0,
      is_builtin: false, document: {}, dependencies: {}, dependency_build_state: 'NOT_REQUIRED',
      dependency_build_error: null,
    },
  ];
  await mockRepository(page, 'capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(capabilities),
  }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/v1/capabilities/skill-version/files', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ digest: 'a'.repeat(64), files: [{ path: 'SKILL.md', editable: true }], compiled_content: null }),
  }));
  await page.route('**/api/v1/capabilities/mcp-version/mcp-source', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      capability_key: 'layout-mcp',
      content: JSON.stringify({ layoutMcp: { url: 'https://mcp.example.com', transport: 'streamable-http' } }),
      mcp_scripts: [],
    }),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();

  const verifyEditor = async (dialogName: string, headingName: string) => {
    const backdrop = page.locator(dialogName.startsWith('编辑 Skill') ? '.capability-package-backdrop' : '.capability-editor-backdrop');
    const dialog = page.getByRole('dialog', { name: dialogName });
    const bounds = await dialog.evaluate(element => {
      const heading = element.querySelector('h2');
      const dialogBounds = element.getBoundingClientRect();
      const headingBounds = heading?.getBoundingClientRect();
      return { dialogBounds, headingBounds };
    });

    await expect(backdrop).toHaveCSS('z-index', dialogName.startsWith('编辑 Skill') ? '150' : '60');
    expect(bounds.dialogBounds.top).toBeGreaterThanOrEqual(0);
    expect(bounds.headingBounds?.top).toBeGreaterThanOrEqual(bounds.dialogBounds.top);
    expect(bounds.headingBounds?.bottom).toBeLessThanOrEqual(bounds.dialogBounds.bottom);
    await expect(dialog.getByRole('heading', { name: headingName })).toBeVisible();
    await expect(dialog.getByRole('button', { name: '关闭' })).toBeVisible();
  };

  const skillCard = page.locator('.capability-card').filter({ hasText: 'layout-skill' });
  await skillCard.getByRole('button', { name: '更多操作：Skill layout-skill' }).click();
  const skillMenu = page.getByRole('menu', { name: 'Skill layout-skill 操作菜单' });
  await expect(skillMenu).toBeVisible();
  await skillMenu.getByRole('menuitem', { name: '编辑' }).click();
  await verifyEditor('编辑 Skill layout-skill', 'layout-skill');
  await page.getByRole('dialog', { name: '编辑 Skill layout-skill' }).getByRole('button', { name: '关闭' }).click();

  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /MCP/ }).click();
  const mcpCard = page.locator('.capability-card').filter({ hasText: 'layout-mcp' });
  await mcpCard.getByRole('button', { name: '更多操作：MCP layout-mcp' }).click();
  const mcpMenu = page.getByRole('menu', { name: 'MCP layout-mcp 操作菜单' });
  await expect(mcpMenu).toBeVisible();
  const mcpMenuBounds = await mcpMenu.boundingBox();
  expect(mcpMenuBounds?.width).toBeGreaterThanOrEqual(156);
  expect((mcpMenuBounds?.y ?? 0) + (mcpMenuBounds?.height ?? 0)).toBeLessThanOrEqual(720);
  await mcpMenu.getByRole('menuitem', { name: '编辑' }).click();
  await verifyEditor('编辑 MCP', '编辑 MCP Server');
});

test('Skill collection editor keeps its heading and actions visible while only skills scroll', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 640 });
  const skills = Array.from({ length: 25 }, (_, index) => ({
    id: `skill-${index + 1}`,
    lineage_id: `skill-lineage-${index + 1}`,
    revision_number: 1,
    is_latest: true,
    capability_type: 'SKILL',
    capability_key: `collection-skill-${index + 1}`,
    description: `Skill ${index + 1} for collection layout coverage`,
    version: '1.0.0',
    filename: `collection-skill-${index + 1}.zip`,
    content_hash: `${index + 1}`.padStart(64, '0'),
    byte_size: 128,
    import_id: `skill-import-${index + 1}`,
    created_at: '2026-09-02T00:00:00Z',
    reference_count: 0,
    is_builtin: false,
    document: {},
    dependencies: {},
    dependency_build_state: 'NOT_REQUIRED',
    dependency_build_error: null,
  }));
  await mockRepository(page, 'capabilities', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(skills),
  }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({
    status: 200, contentType: 'application/json', body: '[]',
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Skill 组合/ }).click();
  await page.getByRole('button', { name: '新建 Skill 组合' }).click();

  const editor = page.locator('form.capability-collection-editor');
  const members = editor.locator('.capability-collection-members');
  const memberList = members.locator(':scope > div');
  const bounds = await editor.evaluate(dialog => {
    const header = dialog.querySelector('header');
    const footer = dialog.querySelector('footer');
    const memberList = dialog.querySelector('.capability-collection-members > div');
    return {
      dialog: dialog.getBoundingClientRect(),
      header: header?.getBoundingClientRect(),
      footer: footer?.getBoundingClientRect(),
      memberList: memberList ? {
        clientHeight: memberList.clientHeight,
        scrollHeight: memberList.scrollHeight,
      } : null,
    };
  });

  expect(bounds.header?.top).toBeGreaterThanOrEqual(0);
  expect(bounds.footer?.bottom).toBeLessThanOrEqual(640);
  expect(bounds.memberList?.scrollHeight).toBeGreaterThan(bounds.memberList?.clientHeight ?? 0);
  await expect(memberList.locator('label').first()).toBeVisible();
  await expect(editor.getByRole('button', { name: '保存组合' })).toBeVisible();
  await expect(editor.getByLabel('分类', { exact: true })).toHaveCount(0);
  await expect(editor.locator('textarea')).toHaveCount(0);
  for (const [width, height] of [[1440, 900], [1280, 640], [1024, 768]]) {
    await page.setViewportSize({ width, height });
    const fields = await editor.locator('.capability-collection-fields input').evaluateAll(inputs => inputs.map(input => input.getBoundingClientRect().toJSON()));
    expect(fields).toHaveLength(2);
    expect(Math.abs(fields[0].top - fields[1].top)).toBeLessThan(1);
    const dialogBox = (await editor.boundingBox())!;
    const listBox = (await members.boundingBox())!;
    expect(listBox.height / dialogBox.height).toBeGreaterThan(0.65);
    expect(await editor.evaluate(element => element.scrollHeight - element.clientHeight)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await expect(editor.getByRole('button', { name: '保存组合' })).toBeInViewport();
    await page.screenshot({ path: `../../.tmp/ops31/collection-editor-${width}-${height}.png` });
  }
});

test('Skill collections have their own paginated table, sort, and row actions', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 480 });
  const collections = Array.from({ length: 23 }, (_, index) => ({
    id: `collection-${String(index).padStart(2, '0')}`, name: `组合 ${String(index).padStart(2, '0')}`,
    category: '研发', description: '批量选择真实 Skill 版本', row_version: 1, members: [],
    created_at: '2026-09-02T00:00:00Z', updated_at: '2026-09-02T00:00:00Z',
  }));
  await mockRepository(page, 'capabilities', route => route.fulfill({ body: '[]' }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ body: JSON.stringify(collections) }));
  await page.goto('/'); await page.getByRole('button', { name: '能力仓库' }).click();
  await expect(page.getByRole('button', { name: '新建 Skill 组合' })).toHaveCount(0);
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Skill 组合/ }).click();
  const rows = page.locator('.capability-table tbody tr');
  await expect(rows).toHaveCount(20);
  await page.getByRole('button', { name: '更多操作：Skill 组合 组合 00' }).click();
  const menu = page.getByRole('menu');
  await expect(menu.getByRole('menuitem', { name: '编辑' })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: '删除' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(rows).toHaveCount(3); await expect(rows.first()).toContainText('组合 20');
  await expect(page.getByRole('button', { name: '上一页' })).toBeEnabled();
  const request = page.waitForRequest(request => request.url().includes('/capability-collection-directory?') && request.url().includes('sort=name') && request.url().includes('page=1'));
  await page.getByRole('columnheader', { name: '名称' }).getByRole('button').click();
  await request; await expect(rows).toHaveCount(20);
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

  await mockRepository(page, 'capabilities', async route => {
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
    await modules.getByRole('button', { name: new RegExp(label === 'Skill' ? '^Skill\\s+\\d' : label) }).click();
    const bulkDelete = page.getByRole('button', { name: '批量删除 (0)' });
    await expect(bulkDelete).toBeDisabled();
    const checkboxes = page.getByRole('checkbox', { name: new RegExp(`选择能力 bulk-delete-${capabilityType.toLowerCase()}-`) });
    await expect(checkboxes).toHaveCount(2);
    const firstCheckbox = checkboxes.nth(0);
    const row = page.locator('.capability-card').filter({ has: page.getByRole('checkbox', { name: await firstCheckbox.getAttribute('aria-label') ?? '', exact: true }) });
    // Description, file, size, version, timestamp, references and status are all
    // selection targets; native controls must still toggle exactly once.
    for (const cell of [1, 2, 3, 4, 5, 6, 7]) {
      const target = cell === 1 ? row.locator('td').nth(cell).locator('small') : row.locator('td').nth(cell);
      await target.click();
      await expect(checkboxes.nth(0)).toBeChecked();
      await expect(page.getByRole('button', { name: '批量删除 (1)' })).toBeEnabled();
      await target.click();
      await expect(checkboxes.nth(0)).not.toBeChecked();
    }
    await checkboxes.nth(0).click();
    await expect(checkboxes.nth(0)).toBeChecked();
    await row.getByRole('button', { name: /^更多操作：/ }).click();
    await expect(page.getByRole('menu')).toBeVisible();
    await expect(checkboxes.nth(0)).toBeChecked();
    await page.keyboard.press('Escape');
    await checkboxes.nth(0).click();
    await expect(checkboxes.nth(0)).not.toBeChecked();
    await checkboxes.nth(0).focus();
    await page.keyboard.press('Space');
    await expect(checkboxes.nth(0)).toBeChecked();
    await page.keyboard.press('Space');
    await expect(checkboxes.nth(0)).not.toBeChecked();
    if (capabilityType === 'CONTEXT') {
      const builtin = page.getByRole('checkbox', { name: '选择能力 builtin-default-context' });
      await expect(builtin).toBeDisabled();
      await page.locator('.capability-card').filter({ has: builtin }).locator('.capability-file-cell').click();
      await expect(builtin).not.toBeChecked();
      await expect(page.getByRole('button', { name: '批量删除 (0)' })).toBeDisabled();
    }
    await page.getByRole('button', { name: '全选当前模块' }).click();
    await expect(checkboxes.nth(0)).toBeChecked();
    await expect(checkboxes.nth(1)).toBeChecked();
    await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
    await page.getByRole('button', { name: '批量删除 (2)' }).click();
    const confirm = page.getByRole('alertdialog');
    await expect(confirm).toContainText('删除所选的 2 项能力');
    await expect(confirm).toContainText('已有会话或运行引用的冻结快照会被保留');
    await confirm.getByRole('button', { name: '确认删除' }).click();
    await expect(checkboxes).toHaveCount(0);
    await expect(page.getByRole('status')).toContainText('已删除 2 条无关联能力记录');
  }
});

test('Skill list keeps pagination in view on short screens and offers a package download', async ({ page }) => {
  const now = new Date().toISOString();
  const skills = Array.from({ length: 25 }, (_, index) => ({
    id: `skill-${index}`, lineage_id: `lineage-${index}`, revision_number: 1, is_latest: true,
    capability_type: 'SKILL', capability_key: `skill-${index}`, description: '测试 Skill', version: '',
    filename: `skill-${index}.zip`, content_hash: `hash-${index}`, digest: `digest-${index}`, byte_size: 1024,
    import_id: `import-${index}`, created_at: now, reference_count: 0, dependencies: {},
    dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null, is_builtin: false, document: null,
  }));
  await mockRepository(page, 'capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(skills) }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.setViewportSize({ width: 1440, height: 600 });
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();

  const pager = page.locator('.capabilities-page .pagination');
  await expect(pager).toBeVisible();
  const box = await pager.boundingBox();
  expect(box && box.y + box.height).toBeLessThanOrEqual(600);
  await expect(page.getByRole('button', { name: '下一页' })).toBeInViewport();
  // The table absorbs the remaining height and scrolls instead of
  // pushing the last row or the pager below the clipped page.
  const grid = await page.locator('.capabilities-page .capability-table-scroll').boundingBox();
  expect(grid && box && grid.y + grid.height).toBeLessThanOrEqual(box!.y + 1);

  const skillCard = page.locator('.capability-card').filter({ hasText: 'skill-0' });
  await skillCard.getByRole('button', { name: '更多操作：Skill skill-0' }).click();
  const download = page.getByRole('menu', { name: 'Skill skill-0 操作菜单' }).getByRole('menuitem', { name: '下载 Skill skill-0' });
  await expect(download).toHaveAttribute('href', /\/api\/v1\/capabilities\/skill-0\/package$/);
});

test('repository requests global sort and preserves selection across button pages', async ({ page }) => {
  const skills = Array.from({ length: 25 }, (_, index) => ({
    id: `paged-version-${index}`, lineage_id: `paged-lineage-${index}`, revision_number: 1, is_latest: true,
    capability_type: 'SKILL', capability_key: `workspace-skill-${String(24 - index).padStart(2, '0')}`,
    description: '提供可复用的工作方法、验收步骤与团队知识，使用时冻结实际能力版本。', version: '1.0.0',
    filename: `workspace-skill-${index}.zip`, content_hash: 'a'.repeat(64), byte_size: 2048 + index * 512,
    import_id: `import-${index}`, created_at: new Date(2026, 8, index + 1).toISOString(), reference_count: index % 3,
    dependencies: {}, dependency_build_state: index % 2 ? 'READY' : 'NOT_REQUIRED', is_builtin: false, document: {},
  }));
  await mockRepository(page, 'capabilities', route => route.fulfill({ body: JSON.stringify(skills) }));
  await mockRepository(page, 'capability-collections', route => route.fulfill({ body: '[]' }));
  await page.goto('/'); await page.getByRole('button', { name: '能力仓库' }).click();
  await expect(page.locator('.capability-table tbody tr')).toHaveCount(20);
  await expect(page.locator('.capability-table tbody tr').first()).toContainText('workspace-skill-00');
  const first = page.getByRole('checkbox', { name: '选择能力 workspace-skill-00' });
  await first.check();
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.locator('.capability-table tbody tr')).toHaveCount(5);
  await expect(page.getByRole('button', { name: '批量删除 (1)' })).toBeEnabled();
  const descending = page.waitForRequest(request => request.url().includes('/capability-directory?') && request.url().includes('sort=name') && request.url().includes('direction=desc') && request.url().includes('page=1'));
  await page.getByRole('columnheader', { name: '名称' }).getByRole('button').click();
  await page.getByRole('columnheader', { name: '名称' }).getByRole('button').click();
  await descending;
  await expect(page.locator('.capability-table tbody tr').first()).toContainText('workspace-skill-24');
  await page.getByRole('button', { name: '全选当前模块' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (25)' })).toBeEnabled();
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.getByRole('checkbox', { name: '选择能力 workspace-skill-00' })).toBeChecked();
  await page.getByRole('button', { name: '取消全选' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (0)' })).toBeDisabled();
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '上一页' }).click();
  await page.getByRole('columnheader', { name: '上传时间' }).getByRole('button').click();
  await page.getByRole('columnheader', { name: '上传时间' }).getByRole('button').click();
  await expect(page.locator('.capability-table tbody tr').first()).toContainText('workspace-skill-00');
  const bounds = await page.locator('.capability-table-scroll').evaluate(element => ({
    client: element.clientHeight, scroll: element.scrollHeight, page: document.documentElement.scrollHeight, viewport: window.innerHeight,
  }));
  expect(bounds.scroll).toBeGreaterThan(bounds.client);
  expect(bounds.page).toBeLessThanOrEqual(bounds.viewport);
  await page.screenshot({ path: '../../.tmp/ops25/capability-repository.png' });
});
