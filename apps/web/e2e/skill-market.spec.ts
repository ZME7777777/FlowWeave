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
  await page.getByRole('button', { name: '导入 Skill', exact: true }).click();
  return page.getByRole('dialog', { name: '导入 Skill', exact: true });
}
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({ json: { id: 'market-user', username: 'market-user', role: 'USER' } }));
  await page.route('**/api/v1/capability-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20, counts: {}, collection_total: 0 } }));
  await page.route('**/api/v1/capability-collection-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } }));
  await page.route('**/api/v1/market-sources', route => route.fulfill({ json: { revision: '1', sources: builtins } }));
  await page.route('**/api/v1/market-sources/skills-sh/skills?*', route => route.fulfill({ json: { skills, search_required: false } }));
});

test('unified source manager shows built-ins and persists custom source for plugin switching', async ({ page }) => {
  let saved = { revision: '1', sources: builtins };
  await page.route('**/api/v1/market-sources', async route => {
    if (route.request().method() === 'PUT') saved = { ...route.request().postDataJSON(), revision: '2' };
    await route.fulfill({ json: saved });
  });
  const dialog = await open(page);
  await expect(dialog.getByRole('link', { name: 'https://skills.sh', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '市场源管理' }).click();
  const manager = page.getByRole('dialog', { name: '市场源管理', exact: true });
  await expect(manager.getByLabel('来源地址 openhands')).toHaveValue('https://github.com/OpenHands/extensions.git');
  await expect(manager.getByLabel('来源地址 openhands')).toHaveAttribute('readonly');
  await manager.getByRole('button', { name: '添加自定义来源' }).click();
  await manager.getByRole('textbox', { name: /^来源名称 custom-/ }).fill('Team extensions');
  await manager.getByRole('combobox', { name: /^来源类型 custom-/ }).selectOption('PLUGIN_GIT');
  await manager.getByRole('textbox', { name: /^来源地址 custom-/ }).fill('https://github.com/acme/extensions.git');
  await manager.getByRole('button', { name: '保存来源' }).click();
  await expect(manager).toBeHidden();
  expect(saved.sources).toHaveLength(3);
  await dialog.getByRole('button', { name: '关闭导入' }).click();
  await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Plugin/ }).click();
  await page.route('**/api/v1/plugin-marketplace-catalogs/openhands', route => route.fulfill({ json: { plugins: [], source: builtins[0].url, commit: sha } }));
  await page.route('**/api/v1/market-sources/*/plugins', route => route.fulfill({ json: { plugins: [{ name: 'team-review' }], source: 'https://github.com/acme/extensions.git', commit: sha } }));
  await page.getByRole('button', { name: '导入 Plugin', exact: true }).click();
  const plugin = page.getByRole('dialog', { name: '导入 Plugin', exact: true });
  await plugin.getByLabel('市场来源').selectOption({ label: 'Team extensions' });
  await expect(plugin.getByRole('checkbox', { name: '选择插件 team-review' })).toBeVisible();
  await expect(plugin.getByRole('link', { name: 'https://github.com/acme/extensions.git' })).toBeVisible();
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
  await expect(dialog.getByRole('button', { name: '搜索', exact: true })).toBeDisabled();
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
  await dialog.getByLabel('市场来源').selectOption('team'); release();
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
  await dialog.getByRole('button', { name: '关闭导入' }).click(); release();
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
