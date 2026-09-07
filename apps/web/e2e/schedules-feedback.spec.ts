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

test('schedule rows expand across their full width and immediate trigger reports progress', async ({ page }) => {
  let triggerAttempts = 0;
  const schedule = {
    id: 'schedule-1', flow_definition_id: 'flow-1', environment_version_id: 'env-version-1',
    name: '每小时检查', source_flow_run_id: 'automatic-master-1', run_mode: 'AUTOMATIC',
    start_node_key: 'first', interval_minutes: 1, cron_expression: '0 * * * *',
    source_flow_run: { id: 'automatic-master-1', name: '连续运行母版', run_no: 2, state: 'DRAFT' },
    status: 'ACTIVE', next_run_at: '2026-09-07T10:00:00Z', row_version: 1,
    config_version: 1, last_run_at: null, has_execution: false,
    created_at: '2026-09-07T09:00:00Z', updated_at: '2026-09-07T09:00:00Z',
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' && request.method() === 'GET') return respond([schedule]);
    if (path === '/api/v1/flows') return respond([{ id: 'flow-1', name: '行情巡检流程' }]);
    if (path === '/api/v1/flow-run-schedule-templates') return respond([]);
    if (path === `/api/v1/flow-run-schedules/${schedule.id}/trigger`) {
      triggerAttempts += 1;
      if (triggerAttempts === 2) {
        return respond({ error: { code: 'SCHEDULE_TRIGGER_FAILED', message: '定时任务启动失败' } }, 500);
      }
      await new Promise(resolve => setTimeout(resolve, 120));
      return respond(schedule, 202);
    }
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时任务', exact: true }).click();
  const flowToggle = page.locator('.schedule-flow-group > header .schedule-tree-toggle');
  const flowHeader = page.locator('.schedule-flow-group > header');
  await expect(flowToggle).toHaveAttribute('aria-expanded', 'false');
  const [buttonBox, headerBox] = await Promise.all([flowToggle.boundingBox(), flowHeader.boundingBox()]);
  expect(buttonBox?.width).toBeGreaterThan((headerBox?.width ?? 0) - 2);
  await flowToggle.click({ position: { x: (buttonBox?.width ?? 20) - 8, y: 8 } });
  await expect(flowToggle).toHaveAttribute('aria-expanded', 'true');
  const masterToggle = page.locator('.schedule-master-group > header .schedule-tree-toggle');
  await masterToggle.click({ position: { x: (await masterToggle.boundingBox())!.width - 8, y: 8 } });

  const trigger = page.getByRole('button', { name: '立即运行', exact: true });
  await trigger.click();
  await expect(page.getByRole('button', { name: '启动中…', exact: true })).toBeDisabled();
  await expect(page.getByRole('status')).toHaveText('已创建连续运行记录，后台正在启动。');
  await trigger.click();
  await expect(page.getByRole('alert')).toHaveText('定时任务启动失败');
});
