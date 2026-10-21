import { expect, test, type Route } from '@playwright/test';

const now = '2026-10-21T02:00:00Z';
const runId = 'history-run';
const attemptId = 'history-attempt';
const bindingId = 'history-binding';
const base = `/api/v1/flow-runs/${runId}/node-attempts/${attemptId}/agent-sessions`;
const routePath = `/flow-runs/${runId}/nodes/history-node/attempts/${attemptId}/agent-sessions/${bindingId}`;

async function json(route: Route, body: unknown) {
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

for (const marker of ['FLOWWEAVE_BUSINESS_RESULT_CONTRACT_V1', 'FLOWWEAVE_UPSTREAM_GATE_AUDIT_V1']) {
  test(`node startup history remains visible with appended ${marker}`, async ({ page }) => {
    const conversation = {
      id: bindingId, display_title: '节点历史回归', title_state: 'MANUAL', lifecycle: 'ACTIVE',
      streaming_callback_ready: true, write_available: true, execution_status: 'finished',
      created_at: now, updated_at: now,
    };
    const event = (id: string, eventType: string, payload: Record<string, unknown>) => ({
      id, event_type: eventType, payload: { timestamp: now, ...payload },
    });
    const batch = {
      events: [
        event('internal-audit', 'MESSAGE', {
          source: 'user', parent_id: '__root__',
          content: '\n\n[FLOWWEAVE_UPSTREAM_GATE_AUDIT_V1]\n隐藏的内部审计记录',
        }),
        event('startup-user', 'MESSAGE', {
          source: 'user', parent_id: 'internal-audit',
          content: `请检查输入并生成节点报告。\n\n[${marker}]\n受控的节点补充约定`,
        }),
        event('startup-tool', 'TOOL_CALL', {
          source: 'agent', parent_id: 'startup-user', event_name: 'ExecuteBashAction',
          tool_call_id: 'history-tool-call', action: { command: 'ls reports' },
        }),
        event('startup-result', 'TOOL_RESULT', {
          source: 'environment', parent_id: 'startup-tool', action_id: 'startup-tool',
          event_name: 'ExecuteBashObservation', tool_call_id: 'history-tool-call', content: '已读取输入报告',
        }),
        event('startup-reply', 'MESSAGE', {
          source: 'agent', parent_id: 'startup-result', final_reply: true, content: '节点报告已生成。',
        }),
      ],
      next_cursor: 'startup-reply', history_cursor: null,
      result: { status: 'COMPLETED', completion_event_id: 'startup-reply', completion_event_kind: 'ASSISTANT_MESSAGE' },
    };
    const readiness = { ready: true, execution_status: 'finished' };
    const context = { used_tokens: 200, window_tokens: 128_000, usage_current: true };
    await page.routeWebSocket('**/agent-sessions/**/stream', () => undefined);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/auth/me')) return json(route, {
        id: 'history-user', username: 'history-user', role: 'USER', can_use_agent_sessions: true,
      });
      if (path === `${base}/host`) return json(route, {
        id: runId, display_name: '节点历史工作区', desired_state: 'RUNNING', updated_at: now,
      });
      if (path === base) return json(route, { items: [conversation], next_cursor: null });
      if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
      if (path.endsWith('/activity')) return json(route, {
        running_binding_ids: [], condensing_binding_ids: [], condensation_failed_binding_ids: [],
        possibly_stuck_binding_ids: [], failed_binding_ids: [],
      });
      if (path.endsWith('/hydration')) return json(route, { events: batch, context, readiness });
      if (path.endsWith('/events')) return json(route, batch);
      if (path.endsWith('/input-readiness')) return json(route, readiness);
      if (path.endsWith('/context')) return json(route, context);
      if (path.endsWith('/work-directories')) return json(route, {
        root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
      });
      if (path.endsWith('/workspace/git/repositories')) return json(route, { repositories: [] });
      if (path.endsWith('/workspace/directory')) return json(route, { entries: [], next_cursor: null });
      if (path.endsWith('/workspace')) return json(route, {
        root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' },
        working_directory: '/runtime/workspace/project', work_directory: null,
        files: [], repositories: [], runtime: {},
        ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } },
      });
      if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
      if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
      if (path === `${base}/${bindingId}`) return json(route, conversation);
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":{"code":"RESOURCE_NOT_FOUND","message":"not found"}}' });
    });

    const assertHistory = async () => {
      const userMessage = page.locator('[data-user-event-id="startup-user"]');
      await expect(userMessage).toContainText('请检查输入并生成节点报告。');
      await expect(page.getByText('节点报告已生成。', { exact: true })).toBeVisible();
      await expect(page.locator('[data-user-event-id="internal-audit"]')).toHaveCount(0);
      await expect(page.getByText('隐藏的内部审计记录', { exact: false })).toHaveCount(0);
      await expect(page.getByRole('separator', { name: '工作过程结束' })).toBeVisible();
      const process = page.getByLabel('展开工作过程', { exact: true });
      await expect(process).toHaveCount(1);
      await process.click();
      await page.getByLabel('查看执行详情：ExecuteBash · 已完成', { exact: true }).click();
      await expect(page.getByText('已读取输入报告', { exact: false })).toBeVisible();
    };
    await page.goto(routePath);
    await assertHistory();
    await page.reload();
    await assertHistory();
  });
}
