import { expect, test, type Page } from '@playwright/test';

const now = '2026-09-30T09:30:00Z';
const user = { id: '00000000-0000-0000-0000-000000000062', username: 'read-coordination', role: 'USER', is_super_admin: false };
const root = '/runtime/workspace/project';

function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

async function foreground(page: Page) {
  await page.evaluate(() => {
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });
}

async function setup(page: Page, node: boolean, options: {
  running?: boolean;
  history?: boolean;
  historyOnRecovery?: boolean;
  hydrate?: (bindingId: string) => Promise<void>;
  missingHydration?: string;
  events?: (url: URL) => Promise<void>;
} = {}) {
  const base = node
    ? '/api/v1/flow-runs/coordination-run/node-attempts/coordination-attempt/agent-sessions'
    : '/api/v1/agent-workspaces/coordination-workspace/conversations';
  const hostBase = node ? base : '/api/v1/agent-workspaces/coordination-workspace';
  const path = (bindingId: string) => node
    ? `/flow-runs/coordination-run/nodes/coordination-node/attempts/coordination-attempt/agent-sessions/${bindingId}`
    : `/agent/conversations/${bindingId}`;
  const conversations = ['a', 'b', 'c'].map(id => ({
    id, display_title: `协调会话 ${id}`, title_state: 'MANUAL', lifecycle: 'ACTIVE',
    streaming_callback_ready: true, write_available: true, execution_status: options.running ? 'running' : 'idle',
    capabilities: [], created_at: now, updated_at: now,
  }));
  const hydrationReads: string[] = [];
  const eventReads: URL[] = [];
  let historyAvailable = !options.historyOnRecovery;
  let activeEvents = 0;
  let maxActiveEvents = 0;
  const batch = (id: string) => ({
    events: [{ id: `${id}-user`, event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: `正式问题 ${id}`, timestamp: now } },
      ...(!options.running ? [{ id: `${id}-reply`, event_type: 'MESSAGE', payload: { source: 'agent', parent_id: `${id}-user`, content: `正式回复 ${id}`, timestamp: now } }] : [])],
    next_cursor: `${id}-leaf`, history_cursor: options.history && historyAvailable ? 'older-1' : null,
    result: { status: options.running ? 'RUNNING' : 'COMPLETED' },
  });
  const readiness = { ready: !options.running, execution_status: options.running ? 'running' : 'idle' };
  const context = { model_name: 'coordination-model', window_tokens: 128_000, used_tokens: 1_024, usage_current: true };
  await page.routeWebSocket('**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (pathname.endsWith('/auth/me')) return json(user);
    if (pathname.endsWith('/agent-workspaces/default') || pathname === `${base}/host`) return json({ id: 'coordination-workspace', display_name: '读取协调', desired_state: 'RUNNING', updated_at: now });
    if (pathname.endsWith('/runtime')) return json({ state: 'ACTIVE', write_available: true, updated_at: now });
    if (pathname === base) return json({ items: conversations, next_cursor: null });
    if (pathname.endsWith('/conversation-activity') || pathname.endsWith('/activity')) return json({ running_binding_ids: options.running ? ['a'] : [] });
    if (pathname.endsWith('/hydration')) {
      const id = pathname.split('/').at(-2)!;
      hydrationReads.push(id);
      await options.hydrate?.(id);
      if (id === options.missingHydration) return json({ error: { code: 'AGENT_CONVERSATION_NOT_FOUND', message: '会话不存在或已删除' } }, 404);
      return json({ events: batch(id), readiness, context });
    }
    if (pathname.endsWith('/events')) {
      eventReads.push(url);
      maxActiveEvents = Math.max(maxActiveEvents, ++activeEvents);
      try {
        await options.events?.(url);
        if (url.searchParams.has('cursor')) historyAvailable = true;
        if (url.searchParams.has('history_cursor')) return json({
          events: [{ id: 'older-1', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '完整历史仍可读取', timestamp: now } }],
          next_cursor: 'a-leaf', history_cursor: null,
        });
        return json(batch(pathname.split('/').at(-2)!));
      } finally { activeEvents -= 1; }
    }
    if (pathname.endsWith('/input-readiness')) return json(readiness);
    if (pathname.endsWith('/context')) return json(context);
    if (pathname.endsWith('/pending-confirmation')) return json({ pending: false });
    if (pathname === `${hostBase}/work-directories`) return json({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: root }, items: [] });
    if (pathname === `${hostBase}/workspace`) return json({ root, scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: root, work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: root, gateway: { supported: false, status: '不可用', note: '' } } });
    if (pathname.endsWith('/model-providers') || pathname.endsWith('/capabilities') || pathname.endsWith('/capability-collections')) return json([]);
    if (pathname.startsWith(`${base}/`)) return json(conversations.find(item => pathname.endsWith(`/${item.id}`)));
    return json({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto(path('a'));
  return { hydrationReads, eventReads, maxActiveEvents: () => maxActiveEvents };
}

for (const node of [false, true]) {
  const host = node ? 'Flow Node' : 'Workspace';
  test(`${host}: foreground signals during hydration wait for the coherent first screen`, async ({ page }) => {
    const hydration = gate();
    const reads = await setup(page, node, { hydrate: () => hydration.promise });
    await expect.poll(() => reads.hydrationReads).toEqual(['a']);
    await foreground(page);
    await page.waitForTimeout(500);
    expect(reads.eventReads).toHaveLength(0);
    await expect(page.getByText('正式问题 a', { exact: true })).toHaveCount(0);
    hydration.release();
    await expect(page.getByText('正式回复 a', { exact: true })).toBeVisible();
    await expect.poll(() => reads.eventReads.length).toBe(1);
    expect(reads.eventReads[0].searchParams.has('cursor')).toBe(false);
  });

  test(`${host}: unmount aborts an in-flight event recovery without publishing its late result`, async ({ page }) => {
    const eventRead = gate();
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window);
      const observed = window as typeof window & { abortedEventReads: number };
      observed.abortedEventReads = 0;
      window.fetch = (input, init) => {
        if (String(input).split('?')[0].endsWith('/events')) {
          init?.signal?.addEventListener('abort', () => { observed.abortedEventReads += 1; }, { once: true });
        }
        return originalFetch(input, init);
      };
    });
    const reads = await setup(page, node, { running: true, events: () => eventRead.promise });
    await expect.poll(() => reads.eventReads.length).toBe(1);
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '节点资产', exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as typeof window & { abortedEventReads: number }).abortedEventReads)).toBe(1);
    // Fulfill after cleanup too: the result must not resurrect the old workbench.
    eventRead.release();
    await expect(page.getByText('正式问题 a', { exact: true })).toHaveCount(0);
    await page.waitForTimeout(500);
    expect(reads.eventReads).toHaveLength(1);
  });

  test(`${host}: incremental recovery queues one latest window and history yields until it completes`, async ({ page }) => {
    const incremental = gate();
    const latest = gate();
    let holdForeground = false;
    const reads = await setup(page, node, {
      running: true, history: true, historyOnRecovery: true,
      events: url => url.searchParams.has('cursor') ? incremental.promise
        : holdForeground && !url.searchParams.has('history_cursor') ? latest.promise : Promise.resolve(),
    });
    await expect(page.getByText('正式问题 a', { exact: true })).toBeVisible();
    await expect.poll(() => reads.eventReads.some(url => url.searchParams.has('cursor'))).toBe(true);
    const histories = () => reads.eventReads.filter(url => url.searchParams.has('history_cursor')).length;
    const before = histories();
    holdForeground = true;
    await foreground(page);
    await page.waitForTimeout(1_700);
    expect(histories()).toBe(before);
    expect(reads.eventReads.filter(url => !url.searchParams.has('cursor') && !url.searchParams.has('history_cursor'))).toHaveLength(1);
    incremental.release();
    await expect.poll(() => reads.eventReads.filter(url => !url.searchParams.has('cursor') && !url.searchParams.has('history_cursor')).length).toBe(2);
    await page.waitForTimeout(1_700);
    expect(histories()).toBe(before);
    latest.release();
    await expect.poll(() => histories()).toBe(1);
    expect(reads.maxActiveEvents()).toBe(1);
    await foreground(page);
    await page.waitForTimeout(1_700);
    expect(histories()).toBe(1);
  });
}

test('Flow Node: a stalled hydration completes before chasing only the final selection', async ({ page }) => {
  const hydration = gate();
  const reads = await setup(page, true, { hydrate: id => id === 'a' ? hydration.promise : Promise.resolve() });
  await expect.poll(() => reads.hydrationReads).toEqual(['a']);
  await page.getByRole('button', { name: '协调会话 b', exact: true }).click();
  await page.getByRole('button', { name: '协调会话 c', exact: true }).click();
  await page.waitForTimeout(500);
  expect(reads.hydrationReads).toEqual(['a']);
  hydration.release();
  await expect(page.getByText('正式回复 c', { exact: true })).toBeVisible();
  expect(reads.hydrationReads).toEqual(['a', 'c']);
  await expect(page.getByText('正式回复 a', { exact: true })).toHaveCount(0);
});

test('A late missing hydration belongs to its original binding after selection changes', async ({ page }) => {
  const hydration = gate();
  const reads = await setup(page, false, { missingHydration: 'a', hydrate: id => id === 'a' ? hydration.promise : Promise.resolve() });
  await expect.poll(() => reads.hydrationReads).toEqual(['a']);
  await page.getByRole('button', { name: '协调会话 c', exact: true }).click();
  await page.waitForTimeout(200);
  hydration.release();
  await expect(page.getByText('正式回复 c', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/agent\/conversations\/c$/);
  expect(reads.hydrationReads).toEqual(['a', 'c']);
});

test('Returning to the binding being hydrated consumes its completion without a second read', async ({ page }) => {
  const hydration = gate();
  const reads = await setup(page, false, { hydrate: () => hydration.promise });
  await expect.poll(() => reads.hydrationReads).toEqual(['a']);
  await page.getByRole('button', { name: '协调会话 b', exact: true }).click();
  await page.getByRole('button', { name: '协调会话 a', exact: true }).click();
  hydration.release();
  await expect(page.getByText('正式回复 a', { exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  expect(reads.hydrationReads).toEqual(['a']);
});
