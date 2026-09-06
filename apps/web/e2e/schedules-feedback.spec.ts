import { expect, test } from '@playwright/test';

const authenticatedUser = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'flowweave',
  role: 'SUPER_ADMIN',
  is_super_admin: true,
};

test('new schedule explains how to create a usable template when none exist', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' || path === '/api/v1/flows' || path === '/api/v1/flow-run-schedule-templates') return respond([]);
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时任务', exact: true }).click();

  const createButton = page.getByRole('button', { name: '新建定时任务', exact: true });
  await expect(createButton).toBeEnabled();
  await createButton.click();
  const notice = page.getByRole('alertdialog');
  await expect(notice).toContainText('当前没有可用的连续运行母版');
  await expect(notice).toContainText('显示“草稿已就绪”后即可新建定时任务');
  await notice.getByRole('button', { name: '我知道了', exact: true }).click();
  await expect(notice).toBeHidden();
});
