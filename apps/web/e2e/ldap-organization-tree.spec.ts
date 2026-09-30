import { expect, test, type Route } from '@playwright/test';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('browses LDAP users by nested organization and preserves authorization controls', async ({ page }) => {
  let aliceEnabled = true;
  const authorizationWrites: Array<{ external_subject: string; enabled: boolean }> = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) {
      return json(route, {
        id: 'admin', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true,
      });
    }
    if (path.endsWith('/auth/ldap-users') && request.method() === 'GET') {
      return json(route, {
        organizations: [
          { id: 'engineering', parent_id: null, name: 'Engineering' },
          { id: 'platform', parent_id: 'engineering', name: 'Platform' },
          { id: 'research', parent_id: 'engineering', name: 'Research' },
          { id: 'finance', parent_id: null, name: 'Finance' },
        ],
        users: [
          { external_subject: 'alice-id', username: 'alice', display_name: 'Alice', email: 'alice@example.test', organization_id: 'platform', enabled: aliceEnabled },
          { external_subject: 'bob-id', username: 'bob', display_name: 'Bob', email: 'bob@example.test', organization_id: 'engineering', enabled: false },
          { external_subject: 'carol-id', username: 'carol', display_name: 'Carol', email: 'carol@example.test', organization_id: 'finance', enabled: false },
        ],
      });
    }
    if (path.endsWith('/auth/ldap-users/enabled') && request.method() === 'PUT') {
      const payload = request.postDataJSON() as { external_subject: string; enabled: boolean };
      authorizationWrites.push(payload);
      aliceEnabled = payload.enabled;
      return json(route, {
        external_subject: payload.external_subject,
        username: 'alice', display_name: 'Alice', email: 'alice@example.test',
        organization_id: 'platform', enabled: payload.enabled,
      });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return json(route, []);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '账户与设置' }).click();
  await page.getByRole('menuitem', { name: '用户管理' }).click();
  await expect(page.getByRole('heading', { name: '用户管理' })).toBeVisible();
  await expect(page.getByText('4', { exact: true }).first()).toBeVisible();

  await page.locator('button.ldap-org-select[title="Engineering"]').click();
  await expect(page.getByText('alice', { exact: true })).toBeVisible();
  await expect(page.getByText('bob', { exact: true })).toBeVisible();
  await expect(page.getByText('carol', { exact: true })).toHaveCount(0);

  await page.locator('button.ldap-org-select[title="Platform"]').click();
  await expect(page.getByText('alice', { exact: true })).toBeVisible();
  await expect(page.getByText('bob', { exact: true })).toHaveCount(0);
  const aliceAuthorization = page.getByLabel('允许 alice 登录');
  await aliceAuthorization.click({ force: true });
  await expect.poll(() => authorizationWrites).toEqual([
    { external_subject: 'alice-id', enabled: false },
  ]);
  await expect(aliceAuthorization).not.toBeChecked();

  await page.locator('button.ldap-org-select[title="Research"]').click();
  await expect(page.getByText('当前组织没有匹配用户')).toBeVisible();
});
