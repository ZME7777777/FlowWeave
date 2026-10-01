import { expect, test, type Route } from '@playwright/test';

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('browses LDAP users by nested organization and preserves authorization controls', async ({ page }) => {
  let aliceEnabled = true;
  let aliceAgentAccess = false;
  let engineeringAgentAccess = false;
  const authorizationWrites: Array<{ external_subject: string; enabled: boolean }> = [];
  const userAgentWrites: Array<{ external_subject: string; enabled: boolean }> = [];
  const organizationAgentWrites: Array<{ organization_id: string; enabled: boolean }> = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) {
      return json(route, {
        id: 'admin', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true,
        can_use_agent_sessions: true,
      });
    }
    if (path.endsWith('/auth/ldap-users') && request.method() === 'GET') {
      return json(route, {
        organizations: [
          { id: 'engineering', parent_id: null, name: 'Engineering', agent_session_direct_access: engineeringAgentAccess, agent_session_access: engineeringAgentAccess },
          { id: 'platform', parent_id: 'engineering', name: 'Platform', agent_session_direct_access: false, agent_session_access: engineeringAgentAccess },
          { id: 'research', parent_id: 'engineering', name: 'Research', agent_session_direct_access: false, agent_session_access: engineeringAgentAccess },
          { id: 'finance', parent_id: null, name: 'Finance', agent_session_direct_access: false, agent_session_access: false },
        ],
        users: [
          { external_subject: 'alice-id', username: 'alice', display_name: 'Alice', email: 'alice@example.test', organization_id: 'platform', enabled: aliceEnabled, agent_session_direct_access: aliceAgentAccess, agent_session_inherited_access: engineeringAgentAccess, agent_session_access: aliceAgentAccess || engineeringAgentAccess },
          { external_subject: 'bob-id', username: 'bob', display_name: 'Bob', email: 'bob@example.test', organization_id: 'engineering', enabled: false, agent_session_direct_access: false, agent_session_inherited_access: engineeringAgentAccess, agent_session_access: engineeringAgentAccess },
          { external_subject: 'carol-id', username: 'carol', display_name: 'Carol', email: 'carol@example.test', organization_id: 'finance', enabled: false, agent_session_direct_access: false, agent_session_inherited_access: false, agent_session_access: false },
        ],
      });
    }
    if (path.endsWith('/auth/ldap-users/agent-session-access') && request.method() === 'PUT') {
      const payload = request.postDataJSON() as { external_subject: string; enabled: boolean };
      userAgentWrites.push(payload);
      aliceAgentAccess = payload.enabled;
      return json(route, {
        external_subject: payload.external_subject,
        username: 'alice', display_name: 'Alice', email: 'alice@example.test',
        organization_id: 'platform', enabled: aliceEnabled,
        agent_session_direct_access: payload.enabled,
        agent_session_inherited_access: engineeringAgentAccess,
        agent_session_access: payload.enabled || engineeringAgentAccess,
      });
    }
    if (path.endsWith('/auth/ldap-organizations/agent-session-access') && request.method() === 'PUT') {
      const payload = request.postDataJSON() as { organization_id: string; enabled: boolean };
      organizationAgentWrites.push(payload);
      engineeringAgentAccess = payload.enabled;
      return route.fulfill({ status: 204, body: '' });
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
  const aliceAgentAuthorization = page.getByLabel('单独允许 alice 使用 Agent 会话');
  await aliceAgentAuthorization.click({ force: true });
  await expect.poll(() => userAgentWrites).toEqual([
    { external_subject: 'alice-id', enabled: true },
  ]);

  const engineeringAgentAuthorization = page.getByLabel('允许 Engineering 组织使用 Agent 会话');
  await engineeringAgentAuthorization.click({ force: true });
  await expect.poll(() => organizationAgentWrites).toEqual([
    { organization_id: 'engineering', enabled: true },
  ]);
  await expect(page.getByText('单独＋组织', { exact: true })).toBeVisible();

  await page.locator('button.ldap-org-select[title="Research"]').click();
  await expect(page.getByText('当前组织没有匹配用户')).toBeVisible();
});

test('hides and rejects the direct Agent session entry for an unapproved user', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) {
      return json(route, {
        id: 'restricted-user', username: 'restricted', role: 'USER',
        is_super_admin: false, can_use_agent_sessions: false,
      });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return json(route, []);
    return json(route, { error: { code: 'AGENT_SESSION_ACCESS_REQUIRED', message: '当前账号未开通 Agent 会话' } }, 403);
  });

  await page.goto('/agent');
  await expect(page.getByRole('button', { name: 'Agent 会话' })).toHaveCount(0);
  await expect(page).not.toHaveURL(/\/agent$/);
  await expect(page.getByText('节点资产', { exact: true }).first()).toBeVisible();
});
