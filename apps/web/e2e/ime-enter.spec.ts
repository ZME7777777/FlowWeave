import { expect, test } from '@playwright/test';

const now = '2026-09-01T00:00:00Z';
const user = { id: 'ime-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const workspace = { id: 'ime-workspace', display_name: '输入法工作区', desired_state: 'RUNNING', updated_at: now };
const conversation = { id: 'ime-conversation', display_title: '检查工作目录', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now };

test('Enter that confirms an input-method candidate does not save the conversation title', async ({ page }) => {
  let authenticated = false;
  let renameRequests = 0;
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const fulfill = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return authenticated ? fulfill(user) : fulfill({ error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return fulfill(user); }
    if (path.endsWith('/agent-workspaces/default')) return fulfill(workspace);
    if (path.endsWith('/runtime')) return fulfill({ state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return fulfill({ items: [conversation], next_cursor: null });
    if (/\/conversations\/[^/]+$/.test(path) && request.method() === 'PATCH') { renameRequests += 1; return fulfill({ ...conversation, display_title: JSON.parse(request.postData() ?? '{}').display_title ?? conversation.display_title }); }
    if (path.endsWith('/hydration')) return fulfill({ events: { events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } }, context: { model_name: 'm', window_tokens: 1000, used_tokens: 1, usage_current: true }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/events')) return fulfill({ events: [], next_cursor: null, history_cursor: null, result: { status: 'COMPLETED' } });
    if (path.endsWith('/input-readiness')) return fulfill({ ready: true, execution_status: 'idle' });
    if (path.endsWith('/pending-confirmation')) return fulfill({ pending: false });
    if (path.endsWith('/context')) return fulfill({ model_name: 'm', window_tokens: 1000, used_tokens: 1, usage_current: true });
    if (path.endsWith('/work-directories')) return fulfill({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return fulfill({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return fulfill([]);
    if (path.includes('/conversations/') && request.method() === 'GET') return fulfill(conversation);
    return fulfill({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill(user.username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByRole('button', { name: '账户与设置' })).toContainText(user.username);
  await page.goto(`/agent/conversations/${conversation.id}`);

  await page.getByRole('navigation', { name: '会话页签' }).getByRole('button').first().dblclick();
  const editor = page.getByLabel('会话标题');
  await editor.fill('分时k线开发实现as da');
  // An IME confirms a candidate with Enter: browsers mark it isComposing / keyCode 229.
  await editor.evaluate(element => {
    for (const init of [{ isComposing: true, keyCode: 13 }, { isComposing: false, keyCode: 229 }]) {
      const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, isComposing: init.isComposing });
      Object.defineProperty(event, 'keyCode', { value: init.keyCode });
      element.dispatchEvent(event);
    }
  });
  await page.waitForTimeout(300);
  expect(renameRequests).toBe(0);
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue('分时k线开发实现as da');
  // A plain Enter still saves.
  await editor.press('Enter');
  await expect.poll(() => renameRequests).toBe(1);
});
