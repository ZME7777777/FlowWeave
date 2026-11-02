import { expect, test, type Page } from '@playwright/test';

const sha = 'a'.repeat(40);
const builtins = [
  { id: 'openhands', name: 'OpenHands', kind: 'PLUGIN_GIT', url: 'https://github.com/OpenHands/extensions.git', ref: 'HEAD', repo_path: '', enabled: true, builtin: true },
  { id: 'skills-sh', name: 'skills.sh', kind: 'SKILLS_SH', url: 'https://skills.sh', ref: 'HEAD', repo_path: '', enabled: true, builtin: true },
];
const skills = ['demo', 'review'].map(name => ({ id: `acme/skills/${name}`, name, skill_id: name, description: `完整技能 ${name}`, repository: 'acme/skills', installs: 123 }));
async function open(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库' }).click();
  await page.getByRole('button', { name: 'Skill 市场', exact: true }).click();
  return page.getByRole('dialog', { name: 'Skill 市场', exact: true });
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({ json: { id: 'market-user', username: 'market-user', role: 'USER' } }));
  await page.route('**/api/v1/capability-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20, counts: {}, collection_total: 0 } }));
  await page.route('**/api/v1/capability-collection-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } }));
  await page.route('**/api/v1/market-sources', route => route.fulfill({ json: { revision: '1', sources: builtins } }));
  await page.route('**/api/v1/market-sources/skills-sh/skills?*', route => route.fulfill({ json: { skills, search_required: false } }));
});

test('each market manages only its own sources with custom controls', async ({ page }) => {
  let saved = { revision: '1', sources: builtins };
  await page.route('**/api/v1/market-sources', async route => {
    if (route.request().method() === 'PUT') saved = { ...route.request().postDataJSON(), revision: '2' };
    await route.fulfill({ json: saved });
  });
  const dialog = await open(page);
  await expect(dialog.getByRole('link', { name: 'https://skills.sh', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  const manager = page.getByRole('dialog', { name: 'Skill 市场源', exact: true });
  await expect(manager.getByText('OpenHands', { exact: true })).toHaveCount(0);
  await expect(manager.locator('select,option,input[type=checkbox]')).toHaveCount(0);
  await manager.getByRole('button', { name: '添加来源' }).click();
  await manager.getByRole('textbox', { name: '来源名称', exact: true }).fill('Team skills');
  await manager.getByRole('textbox', { name: '来源地址', exact: true }).fill('https://github.com/acme/skills.git');
  await manager.getByRole('button', { name: '保存来源' }).click();
  await expect(manager).toBeHidden();
  expect(saved.sources).toHaveLength(3);
  expect(saved.sources.find(item => item.name === 'Team skills')?.kind).toBe('SKILL_GIT');
  await dialog.getByRole('button', { name: '市场来源', exact: true }).click();
  await page.getByRole('option', { name: 'Team skills', exact: true }).click();
  await expect(dialog.getByRole('link', { name: 'https://github.com/acme/skills.git' })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭 Skill 市场' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Plugin/ }).click();
  await page.route('**/api/v1/market-sources/openhands/plugins?*', route => route.fulfill({ json: { plugins: [], source: builtins[0].url, commit: sha } }));
  await page.getByRole('button', { name: '插件市场', exact: true }).click();
  const plugin = page.getByRole('dialog', { name: '插件市场', exact: true });
  await plugin.getByRole('button', { name: '市场源管理' }).click();
  const pluginManager = page.getByRole('dialog', { name: 'Plugin 市场源', exact: true });
  await expect(pluginManager.getByText('OpenHands', { exact: true })).toBeVisible();
  await expect(pluginManager.getByText('Team skills', { exact: true })).toHaveCount(0);
  await expect(pluginManager.getByText('skills.sh', { exact: true })).toHaveCount(0);
  await pluginManager.getByRole('button', { name: '添加来源' }).click();
  await pluginManager.getByRole('textbox', { name: '来源名称', exact: true }).fill('Team plugins');
  await pluginManager.getByRole('textbox', { name: '来源地址', exact: true }).fill('https://github.com/acme/extensions.git');
  await pluginManager.getByRole('button', { name: '保存来源' }).click();
  await expect(pluginManager).toBeHidden();
  expect(saved.sources.find(item => item.name === 'Team plugins')?.kind).toBe('PLUGIN_GIT');
  expect(saved.sources.find(item => item.name === 'Team skills')?.kind).toBe('SKILL_GIT');
});

test('skill search supports click cancel, keyboard selection and batch import', async ({ page }) => {
  const validation: string[] = []; const commits: string[] = [];
  await page.route('**/api/v1/market-skill-imports/validate', async route => {
    const body = route.request().postDataJSON(); validation.push(body.skill_id);
    expect(body.source_id).toBe('skills-sh');
    await route.fulfill({ json: { import_token: body.skill_id } });
  });
  await page.route('**/api/v1/capability-imports', async route => { commits.push(route.request().postDataJSON().import_token); await route.fulfill({ status: 201, json: {} }); });
  const dialog = await open(page);
  await expect(dialog.getByRole('button', { name: '搜索', exact: true })).toBeEnabled();
  await dialog.getByLabel('搜索 Skill').fill('demo');
  await dialog.getByLabel('搜索 Skill').press('Enter');
  const first = dialog.getByRole('checkbox', { name: '选择技能 demo', exact: true });
  await first.click(); await expect(first).toHaveAttribute('aria-checked', 'true');
  await first.click(); await expect(first).toHaveAttribute('aria-checked', 'false');
  await first.press('Space');
  await dialog.getByRole('checkbox', { name: '选择技能 review', exact: true }).click();
  await dialog.getByRole('button', { name: '导入技能 (2)', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('已导入 2 个 Skill');
  expect(validation).toEqual(['demo', 'review']); expect(commits).toEqual(['demo', 'review']);
  await expect(first).toBeDisabled();
});

test('partial failure keeps only failed selection and retry skips completed skills', async ({ page }) => {
  const commits: string[] = []; let fail = true;
  await page.route('**/api/v1/market-skill-imports/validate', route => route.fulfill({ json: { import_token: route.request().postDataJSON().skill_id } }));
  await page.route('**/api/v1/capability-imports', async route => {
    const name = route.request().postDataJSON().import_token; commits.push(name);
    await route.fulfill(name === 'review' && fail ? { status: 503, json: { error: { code: 'TEMPORARY', message: '暂时失败' } } } : { status: 201, json: {} });
  });
  const dialog = await open(page);
  await dialog.getByLabel('搜索 Skill').fill('demo'); await dialog.getByLabel('搜索 Skill').press('Enter');
  for (const name of ['demo', 'review']) await dialog.getByRole('checkbox', { name: `选择技能 ${name}`, exact: true }).click();
  await dialog.getByRole('button', { name: '导入技能 (2)' }).click();
  await expect(dialog.getByRole('alert')).toContainText('暂时失败');
  await expect(dialog.getByRole('button', { name: '导入技能 (1)' })).toBeEnabled();
  fail = false; await dialog.getByRole('button', { name: '导入技能 (1)' }).click();
  await expect(dialog.getByRole('status')).toContainText('已导入 1 个 Skill');
  expect(commits).toEqual(['demo', 'review', 'review']);
});

test('switching sources discards stale search and clears selection', async ({ page }) => {
  const custom = { ...builtins[1], id: 'team', name: 'Team skills', kind: 'SKILL_GIT', url: 'https://github.com/acme/skills.git', builtin: false };
  await page.route('**/api/v1/market-sources', route => route.fulfill({ json: { revision: '1', sources: [...builtins, custom] } }));
  let release: () => void = () => {}; const waiting = new Promise<void>(resolve => { release = resolve; });
  let started = false;
  await page.route('**/api/v1/market-sources/skills-sh/skills?*', async route => { started = true; await waiting; await route.fulfill({ json: { skills, search_required: false } }).catch(() => {}); });
  const dialog = await open(page);
  await dialog.getByLabel('搜索 Skill').fill('demo'); await dialog.getByLabel('搜索 Skill').press('Enter');
  await expect.poll(() => started).toBe(true);
  await dialog.getByRole('button', { name: '市场来源', exact: true }).press('ArrowDown');
  await page.getByRole('option', { name: 'Team skills', exact: true }).click(); release();
  await expect(dialog.getByRole('checkbox')).toHaveCount(0);
  await page.route('**/api/v1/market-sources/team/skills?*', route => route.fulfill({ json: { skills: [{ ...skills[0], commit: sha, path: 'skills/demo' }], search_required: false } }));
  await dialog.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(dialog.getByRole('checkbox')).toHaveCount(1);
  await expect(dialog.getByRole('checkbox')).toHaveAttribute('aria-checked', 'false');
});

test('closing during validation stops publication and remaining batch', async ({ page }) => {
  let release: () => void = () => {}; const waiting = new Promise<void>(resolve => { release = resolve; });
  let validations = 0; let commits = 0;
  await page.route('**/api/v1/market-skill-imports/validate', async route => { validations++; await waiting; await route.fulfill({ json: { import_token: 'demo' } }); });
  await page.route('**/api/v1/capability-imports', async route => { commits++; await route.fulfill({ status: 201, json: {} }); });
  const dialog = await open(page);
  await dialog.getByLabel('搜索 Skill').fill('demo'); await dialog.getByLabel('搜索 Skill').press('Enter');
  for (const name of ['demo', 'review']) await dialog.getByRole('checkbox', { name: `选择技能 ${name}`, exact: true }).click();
  await dialog.getByRole('button', { name: '导入技能 (2)' }).click();
  await expect.poll(() => validations).toBe(1);
  await dialog.getByRole('button', { name: '关闭 Skill 市场' }).click(); release();
  await expect(dialog).toBeHidden();
  await page.waitForTimeout(200);
  expect(validations).toBe(1); expect(commits).toBe(0);
});

test('compact viewport preserves source picker, list and footer', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  const dialog = await open(page);
  await dialog.getByLabel('搜索 Skill').fill('demo'); await dialog.getByLabel('搜索 Skill').press('Enter');
  await expect(dialog.getByRole('checkbox').first()).toBeVisible();
  const box = await dialog.boundingBox(); expect(box!.width).toBeLessThanOrEqual(390); expect(box!.height).toBeLessThanOrEqual(700);
  await expect(dialog.getByRole('button', { name: '导入技能', exact: true })).toBeInViewport();
  await dialog.screenshot({ path: '../../.tmp/ops54-market/skill-import-mobile.png' });
});

test('ZIP import actions open only the matching file picker and preserve validation and confirmation', async ({ page }) => {
  const requests: Array<{ capability_type: string; filename: string }> = [];
  let marketReads = 0; let commits = 0;
  await page.route('**/api/v1/market-sources', route => { marketReads++; return route.fulfill({ json: { revision: '1', sources: builtins } }); });
  await page.route('**/api/v1/capability-imports/validate', async route => {
    requests.push(route.request().postDataJSON());
    await route.fulfill({ json: { import_token: 'zip-token', preview: { capabilities: [{ capability_key: 'example' }], file_count: 2 } } });
  });
  await page.route('**/api/v1/capability-imports', async route => { commits++; await route.fulfill({ status: 201, json: { capabilities: [{ id: 'imported' }] } }); });
  await page.goto('/'); await page.getByRole('button', { name: '能力仓库' }).click();
  for (const kind of ['SKILL', 'PLUGIN'] as const) {
    if (kind === 'PLUGIN') await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Plugin/ }).click();
    const label = kind === 'SKILL' ? 'Skill' : 'Plugin';
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: `导入 ${label}`, exact: true }).click();
    await (await chooser).setFiles({ name: `${kind}.zip`, mimeType: 'application/zip', buffer: Buffer.from('zip fixture') });
    const confirm = page.getByRole('alertdialog', { name: `确认导入 ${kind}.zip` });
    await expect(confirm).toContainText('识别到 1 个');
    await confirm.getByRole('button', { name: '导入 1 项能力', exact: true }).click();
    await expect(page.getByRole('status')).toContainText(`项 ${label} 能力`);
  }
  expect(requests.map(item => item.capability_type)).toEqual(['SKILL', 'PLUGIN']);
  expect(commits).toBe(2); expect(marketReads).toBe(0);
  await expect(page.getByRole('dialog', { name: /市场/ })).toHaveCount(0);
});

test('saving skill sources merges a newer plugin edit and retries one revision conflict', async ({ page }) => {
  let saved = { revision: '1', sources: builtins.map(item => ({ ...item })) };
  let writes = 0;
  await page.route('**/api/v1/market-sources', async route => {
    if (route.request().method() === 'PUT') {
      writes++;
      if (writes === 1) {
        saved = { revision: '3', sources: [...saved.sources, { ...builtins[0], id: 'new-plugin', name: 'New plugins', builtin: false }] };
        return route.fulfill({ status: 409, json: { error: { code: 'MARKET_SOURCES_CONFLICT', message: '版本冲突' } } });
      }
      saved = { ...route.request().postDataJSON(), revision: '4' };
    }
    await route.fulfill({ json: saved });
  });
  const dialog = await open(page);
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  const manager = page.getByRole('dialog', { name: 'Skill 市场源', exact: true });
  await manager.getByRole('switch', { name: '启用来源 skills.sh' }).click();
  saved = { revision: '2', sources: saved.sources.map(item => item.id === 'openhands' ? { ...item, enabled: false } : item) };
  await manager.getByRole('button', { name: '保存来源' }).click();
  await expect(manager).toBeHidden();
  expect(writes).toBe(2);
  expect(saved.sources.find(item => item.id === 'openhands')?.enabled).toBe(false);
  expect(saved.sources.find(item => item.id === 'skills-sh')?.enabled).toBe(false);
  expect(saved.sources.find(item => item.id === 'new-plugin')).toBeTruthy();
  await expect(dialog.getByRole('button', { name: '市场来源', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  await expect(page.getByRole('dialog', { name: 'Skill 市场源', exact: true }).getByRole('switch')).toHaveCount(1);
});

test('competing edits to the same market remain visible and cannot overwrite newer sources', async ({ page }) => {
  let saved = { revision: '1', sources: builtins.map(item => ({ ...item })) };
  let writes = 0;
  await page.route('**/api/v1/market-sources', async route => {
    if (route.request().method() === 'PUT') writes++;
    await route.fulfill({ json: saved });
  });
  const dialog = await open(page);
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  const manager = page.getByRole('dialog', { name: 'Skill 市场源', exact: true });
  await manager.getByRole('switch', { name: '启用来源 skills.sh' }).press('Space');
  saved = { revision: '2', sources: [...saved.sources, { ...builtins[1], id: 'new-skill', name: 'New skills', kind: 'SKILL_GIT', builtin: false }] };
  await manager.getByRole('button', { name: '保存来源' }).click();
  await expect(manager.getByRole('alert')).toContainText('Skill 市场源已在其他页面更新');
  expect(writes).toBe(0);
  await manager.getByRole('button', { name: '重新加载', exact: true }).click();
  await expect(manager.getByText('New skills', { exact: true })).toBeVisible();
  await expect(manager.getByRole('switch', { name: '启用来源 skills.sh' })).toHaveAttribute('aria-checked', 'true');
  await manager.getByRole('button', { name: '删除来源 New skills', exact: true }).click();
  await expect(manager.getByText('New skills', { exact: true })).toHaveCount(0);
  await manager.getByRole('button', { name: '取消', exact: true }).click();
  expect(saved.sources.some(item => item.id === 'new-skill')).toBe(true);
});

for (const width of [1440, 390]) test(`source manager keeps the list and custom controls in bounds at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 700 });
  const dialog = await open(page);
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  const manager = page.getByRole('dialog', { name: 'Skill 市场源', exact: true });
  await manager.getByRole('button', { name: '添加来源' }).click();
  await manager.getByRole('textbox', { name: '来源名称', exact: true }).fill('Team skills');
  await manager.getByRole('textbox', { name: '来源地址', exact: true }).fill('https://github.com/acme/skills.git');
  await expect(manager.locator('select,option,input[type=checkbox]')).toHaveCount(0);
  await expect(manager.getByRole('button', { name: '保存来源', exact: true })).toBeInViewport();
  const bounds = await manager.boundingBox(); expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
  expect(await manager.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await manager.screenshot({ path: test.info().outputPath(`market-sources-${width}.png`) });
  await page.keyboard.press('Escape'); await expect(manager).toBeHidden();
  await dialog.getByRole('button', { name: '市场来源', exact: true }).press('ArrowDown');
  await expect(page.getByRole('listbox', { name: '市场来源' })).toBeVisible();
  await page.keyboard.press('Escape'); await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(dialog).toBeVisible();
});

test('Skill market opens with popular recommendations and can return after a focused search', async ({ page }) => {
  const terms: string[] = [];
  await page.route('**/api/v1/market-sources/skills-sh/skills?*', async route => {
    const term = new URL(route.request().url()).searchParams.get('q') || ''; terms.push(term);
    await route.fulfill({ json: { skills: term ? [skills[1]] : skills, search_required: false, ...(term ? {} : { mode: 'popular' }) } });
  });
  const dialog = await open(page);
  await expect(dialog.getByRole('checkbox', { name: '选择技能 demo', exact: true })).toBeVisible();
  await expect(dialog.getByText(/热门推荐 · 累计安装量/)).toBeVisible();
  expect(terms).toContain('');
  await dialog.getByLabel('搜索 Skill').fill('review');
  await dialog.getByLabel('搜索 Skill').press('Enter');
  await expect(dialog.getByRole('checkbox')).toHaveCount(1);
  await expect(dialog.getByRole('checkbox', { name: '选择技能 review', exact: true })).toBeVisible();
  await dialog.getByLabel('搜索 Skill').fill('');
  await dialog.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(dialog.getByText(/热门推荐 · 累计安装量/)).toBeVisible();
  await expect(dialog.getByRole('checkbox')).toHaveCount(2);
  await dialog.screenshot({ path: test.info().outputPath('skill-popular.png') });
});
