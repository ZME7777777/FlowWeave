import { expect, test, type Page } from '@playwright/test';

const digest = 'a'.repeat(64);
const skill = { id: 'skill-current', lineage_id: 'skill-lineage', revision_number: 2, is_latest: true, capability_type: 'SKILL', capability_key: 'complete-skill', description: 'Files and scripts', version: '', filename: 'complete.zip', content_hash: digest, byte_size: 2500, created_at: '2026-10-28T00:00:00Z', reference_count: 0, is_builtin: false, dependencies: {}, dependency_build_state: 'NOT_REQUIRED', dependency_build_error: null, document: {} };
const context = { ...skill, id: 'context-current', lineage_id: 'context-lineage', capability_type: 'CONTEXT', capability_key: 'product-context', filename: 'context.zip' };
const metadata = (asset: typeof skill, previous = false) => ({ ...asset, id: previous ? `${asset.id}-previous` : asset.id, digest, version_no: previous ? 1 : 2 });
const files = ['SKILL.md', 'scripts/run.py', 'config/defaults.yaml', 'assets/icon.png'];
const sources: Record<string, string> = { 'SKILL.md': '# Skill\nRead the scripts.', 'scripts/run.py': "print('old')\n", 'config/defaults.yaml': 'mode: default\n', 'README.md': '# Overview\nFirst document', 'guides/notes.md': '# Notes\nSecond document' };
async function setup(page: Page, asset = skill) {
  await page.route('**/api/v1/auth/me', route => route.fulfill({ json: { id: 'package-user', username: 'package-user', role: 'USER', is_super_admin: false } }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({ json: [asset] }));
  await page.route('**/api/v1/capability-collections', route => route.fulfill({ json: [] }));
  await page.route('**/api/v1/capability-collection-directory?*', route => route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } }));
  await page.route('**/api/v1/capability-directory?*', route => route.fulfill({ json: { items: [{ id: asset.lineage_id, latest: asset, versions: [asset, { ...asset, id: `${asset.id}-previous`, is_latest: false, revision_number: 1 }] }], total: 1, page: 1, page_size: 20, counts: { [asset.capability_type]: 1 }, collection_total: 0 } }));
  await page.route(`**/api/v1/capabilities/${asset.id}/files`, route => route.fulfill({ json: { ...metadata(asset), entry: asset.capability_type === 'SKILL' ? 'SKILL.md' : '', compiled_content: asset.capability_type === 'CONTEXT' ? '# Total\nFirst document\nSecond document' : null, manifest: asset.capability_type === 'CONTEXT' ? { entrypoint: 'README.md', documents: [{ path: 'README.md', title: 'Overview' }, { path: 'guides/notes.md', title: 'Notes' }], conflict_policy: 'ORDERED_DOCUMENTS_LATER_WINS' } : null, files: (asset.capability_type === 'SKILL' ? files : ['README.md', 'guides/notes.md']).map(path => ({ path, title: path, byte_size: 40, sha256: digest, editable: asset.capability_type === 'SKILL' && !path.endsWith('.png') })) } }));
  await page.route(`**/api/v1/capabilities/${asset.id}/file?*`, route => { const path = new URL(route.request().url()).searchParams.get('path')!; return route.fulfill({ json: { path, content: sources[path] ?? null, byte_size: 40, editable: asset.capability_type === 'SKILL' && !path.endsWith('.png'), preview_unavailable: path.endsWith('.png'), image: null } }); });
  await page.goto('/');
  await page.getByRole('button', { name: '能力仓库', exact: true }).click();
  if (asset.capability_type === 'CONTEXT') await page.getByRole('navigation', { name: '能力模块' }).getByRole('button', { name: /Context/ }).click();
}
async function openMenu(page: Page, name: string) { await page.getByRole('button', { name: `更多操作：${name}`, exact: true }).click(); }

test('Skill full package keeps file drafts and publishes all edits with the frozen digest', async ({ page }) => {
  await setup(page);
  let payload: unknown;
  await page.route(`**/api/v1/capabilities/${skill.id}/files`, async route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    payload = route.request().postDataJSON(); await route.fulfill({ json: { ...skill, id: 'published' } });
  });
  await page.getByRole('button', { name: '编辑 Skill complete-skill', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 Skill complete-skill' });
  await expect(dialog.getByLabel('编辑 SKILL.md')).toHaveValue(sources['SKILL.md']);
  await dialog.getByRole('treeitem', { name: 'run.py', exact: true }).click();
  await dialog.getByLabel('编辑 scripts/run.py').fill("print('new')\n");
  await dialog.getByRole('treeitem', { name: 'defaults.yaml', exact: true }).click();
  await dialog.getByLabel('编辑 config/defaults.yaml').fill('mode: updated\n');
  await dialog.getByRole('treeitem', { name: /run.py/ }).click();
  await expect(dialog.getByLabel('编辑 scripts/run.py')).toHaveValue("print('new')\n");
  await expect(dialog.getByRole('button', { name: '发布新版本（2）' })).toBeEnabled();
  await dialog.getByRole('treeitem', { name: 'icon.png', exact: true }).click();
  await expect(dialog.getByText('该文件为二进制或超过 1 MiB，请下载完整包查看。')).toBeVisible();
  await dialog.getByRole('treeitem', { name: '包配置' }).click();
  await expect(dialog.locator('pre')).toContainText(digest);
  await page.screenshot({ path: '../../.tmp/ops50/skill-package.png' });
  await dialog.getByRole('button', { name: '发布新版本（2）' }).click();
  await expect(dialog).toBeHidden();
  expect(payload).toEqual({ expected_digest: digest, files: { 'scripts/run.py': "print('new')\n", 'config/defaults.yaml': 'mode: updated\n' } });
});

test('package version review uses shared file Diff, both modes, navigation and downloads', async ({ page }) => {
  await setup(page);
  await page.route(`**/api/v1/capabilities/${skill.id}/version-comparison`, route => route.fulfill({ json: { previous: metadata(skill, true), current: metadata(skill), files: [{ path: 'SKILL.md', status: 'unchanged' }, { path: 'scripts/run.py', status: 'modified' }, { path: 'config/new.yaml', status: 'added' }, { path: 'assets/icon.png', status: 'modified' }] } }));
  await page.route(`**/api/v1/capabilities/${skill.id}/file-diff?*`, route => { const path = new URL(route.request().url()).searchParams.get('path')!; const patch = path.endsWith('.py') ? `--- previous/scripts/run.py\n+++ current/scripts/run.py\n@@ -1,4 +1,4 @@\n-print('old')\n+print('new')\n ${'x'.repeat(260)}\n context\n last\n@@ -30,2 +30,2 @@\n tail\n-old tail\n+new tail\n` : '@@ -0,0 +1 @@\n+mode: new\n'; return route.fulfill({ json: { path, diff: patch, preview_unavailable: path.endsWith('.png') } }); });
  await openMenu(page, 'Skill complete-skill');
  await page.getByRole('menuitem', { name: '版本对比', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '版本对比 complete-skill' });
  await expect(dialog.locator('.agent-diff-split')).toBeVisible();
  await expect(dialog.locator('.agent-diff-line.addition').filter({ hasText: "print('new')" })).toBeVisible();
  await expect(dialog.getByRole('treeitem', { name: /SKILL.md/ })).toHaveCount(0);
  await dialog.getByLabel('未变更', { exact: true }).check();
  await expect(dialog.getByRole('treeitem', { name: /SKILL.md/ })).toBeVisible();
  await dialog.getByRole('button', { name: '下一处改动' }).click();
  await dialog.getByRole('button', { name: '下一处改动' }).click();
  await expect(dialog.locator('.agent-diff-split-viewport>.agent-diff-scroll-end-spacer')).toHaveCount(1);
  await dialog.getByRole('button', { name: '统一', exact: true }).click();
  await expect(dialog.locator('.agent-diff-unified .agent-diff-line.deletion').filter({ hasText: "print('old')" })).toBeVisible();
  await expect(dialog.locator('.agent-diff-unified>.agent-diff-scroll-end-spacer')).toHaveCount(1);
  await dialog.getByRole('button', { name: '并排', exact: true }).click();
  await expect(dialog.getByRole('link', { name: '下载上一版本' })).toHaveAttribute('href', /skill-current-previous\/package$/);
  await page.screenshot({ path: '../../.tmp/ops50/skill-diff.png' });
  await dialog.getByRole('treeitem', { name: /icon.png/ }).click();
  await expect(dialog.getByText('二进制或大文件不支持行级对比，可下载两版查看。')).toBeVisible();
});

test('Context opens total content first, switches files and offers comparison, download and rollback', async ({ page }) => {
  await setup(page, context);
  await page.getByRole('button', { name: '查看 Context product-context' }).click();
  const dialog = page.getByRole('dialog', { name: '查看 Context product-context' });
  await expect(dialog.getByRole('treeitem', { name: '总内容', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(dialog.locator('pre')).toContainText('First document\nSecond document');
  await dialog.getByRole('treeitem', { name: 'notes.md', exact: true }).click();
  await expect(dialog.locator('pre')).toHaveText(sources['guides/notes.md']);
  await dialog.getByRole('treeitem', { name: '总内容', exact: true }).click();
  await expect(dialog.locator('pre')).toContainText('# Total');
  await expect(dialog.getByRole('link', { name: '下载 Context product-context' })).toHaveAttribute('href', /context-current\/package$/);
  await page.setViewportSize({ width: 390, height: 780 });
  await page.screenshot({ path: '../../.tmp/ops50/context-mobile.png' });
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openMenu(page, 'Context product-context');
  await expect(page.getByRole('menuitem', { name: '版本对比', exact: true })).toBeEnabled();
  await expect(page.getByRole('menuitem', { name: '下载 Context product-context', exact: true })).toHaveAttribute('href', /context-current\/package$/);
  await page.route(`**/api/v1/capabilities/${context.id}/rollback`, route => route.fulfill({ json: { ...context, id: 'restored' } }));
  await page.getByRole('menuitem', { name: '回退到上一版本' }).click();
  await page.getByRole('button', { name: '确认回退', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已回退 Context');
});

test('slow file reads cannot replace a later selected file and failed publishing keeps drafts', async ({ page }) => {
  await setup(page);
  let release: (() => void) | undefined;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/v1/capabilities/${skill.id}/file?*`, async route => {
    const path = new URL(route.request().url()).searchParams.get('path');
    if (path !== 'scripts/run.py') return route.fallback();
    await gate;
    await route.fulfill({ json: { path, content: 'late script', editable: true } }).catch(() => {});
  });
  await page.getByRole('button', { name: '编辑 Skill complete-skill', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑 Skill complete-skill' });
  await dialog.getByRole('treeitem', { name: 'run.py', exact: true }).click();
  await dialog.getByRole('treeitem', { name: 'defaults.yaml', exact: true }).click();
  await expect(dialog.getByLabel('编辑 config/defaults.yaml')).toHaveValue('mode: default\n');
  release!();
  await expect(dialog.getByLabel('编辑 scripts/run.py')).toHaveCount(0);
  await dialog.getByLabel('编辑 config/defaults.yaml').fill('changed');
  await page.route(`**/api/v1/capabilities/${skill.id}/files`, route => route.fulfill({ status: 409, json: { error: { code: 'CAPABILITY_SOURCE_CONFLICT', message: '能力包已有新版本，请重新打开后编辑。' } } }));
  await dialog.getByRole('button', { name: '发布新版本（1）' }).click();
  await expect(dialog.getByRole('alert')).toContainText('能力包已有新版本');
  await expect(dialog.getByLabel('编辑 config/defaults.yaml')).toHaveValue('changed');
});
