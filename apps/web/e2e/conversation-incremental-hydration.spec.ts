import { expect, test } from '@playwright/test';

for (const node of [false, true]) {
  for (const mode of ['unchanged', 'append', 'detached', 'failed'] as const) {
    test(`${node ? 'Flow Node' : 'Workspace'} hydrates cached history atomically with ${mode} native cursor`, async ({ page }) => {
      const timestamp = '2026-10-26T01:00:00Z';
      const workspace = { id: 'incremental-workspace', display_name: '增量工作区', desired_state: 'RUNNING', updated_at: timestamp };
      const conversation = { id: 'incremental-a', display_title: '增量会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', write_available: true, streaming_callback_ready: true, created_at: timestamp, updated_at: timestamp };
      const other = { ...conversation, id: 'incremental-b', display_title: '其他会话' };
      const base = node ? '/api/v1/flow-runs/incremental-run/node-attempts/incremental-attempt/agent-sessions' : `/api/v1/agent-workspaces/${workspace.id}/conversations`;
      const path = node ? `/flow-runs/incremental-run/nodes/incremental-node/attempts/incremental-attempt/agent-sessions/${conversation.id}` : `/agent/conversations/${conversation.id}`;
      const history = [
        { id: 'history-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '历史问题', timestamp } },
        { id: 'history-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'history-user', content: '历史回复', final_reply: true, timestamp } },
      ];
      const latest = [
        { id: 'latest-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: mode === 'detached' ? '__root__' : 'history-reply', content: '最新问题', timestamp } },
        { id: 'latest-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'latest-user', content: '最新回复', final_reply: true, timestamp } },
      ];
      const context = { model_name: 'test-model', used_tokens: 100, usage_current: true };
      const reads: Array<string | null> = [];
      let failRecovery = mode === 'failed';
      let release!: () => void;
      const gate = new Promise<void>(resolve => { release = resolve; });
      await page.routeWebSocket('**/stream', () => undefined);
      await page.route('**/api/v1/**', async route => {
        const request = route.request();
        const url = new URL(request.url());
        const endpoint = url.pathname;
        const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
        if (endpoint.endsWith('/auth/me')) return json({ id: 'incremental-user', username: 'test-user', role: 'USER', is_super_admin: false });
        if (endpoint.endsWith('/agent-workspaces/default') || endpoint === `${base}/host`) return json(workspace);
        if (endpoint.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: timestamp });
        if (endpoint === base) return json({ items: [conversation, other], next_cursor: null });
        if (endpoint.endsWith('/conversation-activity') || endpoint.endsWith('/activity')) return json({ conversations: [], running_binding_ids: [] });
        if (endpoint.endsWith('/hydration')) {
          const target = endpoint.includes(`/${conversation.id}/`);
          const cursor = url.searchParams.get('cursor');
          if (target) reads.push(cursor);
          const recovering = target && reads.length > 1;
          if (recovering) await gate;
          if (recovering && failRecovery) return json({ error: { code: 'HYDRATION_REJECTED', message: '会话更新暂时失败' } }, 400);
          if (recovering && cursor && mode === 'detached') return json({ error: { code: 'RUNTIME_EVENT_IDENTITY_MISMATCH', message: 'Cursor left the active branch' } }, 409);
          const events = !target ? [] : !recovering ? history : mode === 'unchanged' ? [] : latest;
          return json({
            events: { events, next_cursor: recovering && mode !== 'unchanged' ? 'latest-reply' : target ? 'history-reply' : null, history_cursor: null, result: { status: 'COMPLETED' } },
            ...(cursor ? { incremental_from: cursor } : {}),
            context,
            // An old running lease expires sooner than retained event history.
            readiness: { ready: recovering || !target, execution_status: recovering || !target ? 'idle' : 'running' },
          });
        }
        if (endpoint.endsWith('/events')) return json({ events: [], next_cursor: 'history-reply', history_cursor: null });
        if (endpoint.endsWith('/context')) return json(context);
        if (endpoint.endsWith('/input-readiness')) return json({ ready: true, execution_status: 'idle' });
        if (endpoint.endsWith('/pending-confirmation')) return json({ pending: false });
        if (endpoint.endsWith('/work-directories')) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
        if (endpoint.endsWith('/workspace')) return json({ root: '/runtime/workspace/project', working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, scope: { kind: 'ROOT', display_name: '根工作区' }, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
        if (endpoint.endsWith('/model-providers') || endpoint.endsWith('/capabilities') || endpoint.endsWith('/capability-collections')) return json([]);
        if (endpoint.startsWith(`${base}/`) && request.method() === 'GET') return json(endpoint.endsWith(other.id) ? other : conversation);
        return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
      });
      const initialTime = Date.now();
      await page.clock.install({ time: new Date(initialTime) });
      await page.goto(path);
      await expect(page.getByText('历史回复', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: other.display_title, exact: true }).click();
      await expect(page).toHaveURL(/incremental-b$/);
      await page.clock.setFixedTime(new Date(initialTime + 35_000));
      await page.getByRole('button', { name: conversation.display_title, exact: true }).click();
      await expect.poll(() => reads.length).toBe(2);
      await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toBeVisible();
      await expect(page.getByText('历史回复', { exact: true })).toHaveCount(0);
      await expect(page.getByText('最新回复', { exact: true })).toHaveCount(0);
      await expect(page.getByLabel('发送 Agent 消息')).toHaveCount(0);
      release();
      if (mode === 'failed') {
        await expect(page.getByText('会话暂时无法加载', { exact: true })).toBeVisible();
        await expect(page.getByText('历史回复', { exact: true })).toHaveCount(0);
        await expect(page.getByLabel('发送 Agent 消息')).toHaveCount(0);
        failRecovery = false;
        await page.getByRole('button', { name: '重新读取会话', exact: true }).click();
      }
      await expect(page.getByText(mode === 'unchanged' ? '历史回复' : '最新回复', { exact: true })).toBeVisible();
      await expect(page.getByRole('status').filter({ hasText: '正在加载会话' })).toHaveCount(0);
      await expect(page.getByText('历史回复', { exact: true })).toHaveCount(mode === 'detached' ? 0 : 1);
      expect(reads).toEqual(mode === 'detached' ? [null, 'history-reply', null] : mode === 'failed' ? [null, 'history-reply', 'history-reply'] : [null, 'history-reply']);
      await expect(page.getByLabel('发送 Agent 消息')).toBeEditable();
    });
  }
}
