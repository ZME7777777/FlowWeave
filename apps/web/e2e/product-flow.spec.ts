import { expect, test, type APIRequestContext, type Locator, type Page, type WebSocketRoute } from '@playwright/test';

test.use({ timezoneId: 'Asia/Shanghai' });

const apiBase = process.env.E2E_API_URL ?? 'http://127.0.0.1:8080';
const suffix = Date.now().toString(36);

async function post(request: APIRequestContext, path: string, data: unknown) {
  const response = await request.post(`${apiBase}/api/v1${path}`, { data });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}


async function createAsset(request: APIRequestContext, name: string) {
  return post(request, '/node-assets', {
    name,
    description: '浏览器端到端验收节点',
    icon_kind: 'LUCIDE',
    icon_value: 'bot',
    inputs: [{ field_key: 'prd', display_name: '需求文档', data_type: 'URL', description: '' }],
    outputs: [{ field_key: 'design', display_name: '技术方案', data_type: 'URL', description: '' }],
    executor: {
      startup_prompt: '读取输入并生成方案',
      context_prompt: '保留证据',
      context_capability_ids: [],
    },
  });
}

let readyEnvironmentVersion: Promise<string> | undefined;

async function readyEnvironmentVersionId(request: APIRequestContext) {
  readyEnvironmentVersion ??= (async () => {
    const existing = await request.get(`${apiBase}/api/v1/terminal-environments`);
    expect(existing.ok(), await existing.text()).toBeTruthy();
    const environments = await existing.json() as Array<{ name: string; versions: Array<{ id: string; state: string; runtime_compatible: boolean }> }>;
    // Product-flow tests exercise FlowRun and node-session behavior, not the
    // substantially slower Runtime-image publication path. Reuse any
    // compatible READY environment already present in the deployed stack.
    const published = environments
      .flatMap(environment => environment.versions)
      .find(version => version.state === 'READY' && version.runtime_compatible);
    if (published) return published.id;
    const environment = await post(request, '/terminal-environments', {
      name: `E2E运行环境-${suffix}`,
      description: '端到端验收所需的已发布 OpenHands Runtime',
    });
    const setup = await post(request, `/terminal-environments/${environment.id}/setup-sessions`, {});
    const version = await post(request, `/environment-setup-sessions/${setup.id}/publish`, {});
    expect(version.state).toBe('READY');
    expect(version.runtime_compatible).toBeTruthy();
    return version.id as string;
  })();
  return readyEnvironmentVersion;
}

async function createFlow(request: APIRequestContext, assetId: string, name: string) {
  const gates = [
    { stage: 'START', position: 0, gate_type: 'JAVASCRIPT', enabled: true, timeout_seconds: 30, config: { code: "return {decision: 'PASS', summary: '开始门禁通过', reasons: [], evidence: [], details: {}};" } },
    { stage: 'END', position: 0, gate_type: 'PYTHON', enabled: true, timeout_seconds: 30, config: { code: "result = {'decision': 'PASS', 'summary': '结束门禁通过', 'reasons': [], 'evidence': [], 'details': {}}" } },
  ];
  return post(request, '/flows', {
    name,
    description: '同一资产重复放置并显式映射产物',
    default_entry_key: 'design_a',
    nodes: [
      { instance_key: 'design_a', node_asset_id: assetId, alias: '首轮方案', position_x: 100, position_y: 160, config_override: {}, gates },
      { instance_key: 'design_b', node_asset_id: assetId, alias: '复核方案', position_x: 500, position_y: 160, config_override: {}, gates },
    ],
    edges: [{
      source_instance_key: 'design_a',
      target_instance_key: 'design_b',
      position: 0,
    }],
    port_mappings: [{ source_instance_key: 'design_a', source_output_key: 'design', target_instance_key: 'design_b', target_input_key: 'prd' }],
  });
}

async function login(page: Page) {
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.removeItem('flowweave-workbench');
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith('flowweave.agent.') || key.startsWith('flowweave.node-session.')) sessionStorage.removeItem(key);
    }
  });
  await page.reload();
}

async function confirmProductDialog(
  page: Page,
  trigger: Locator,
  confirmLabel: string,
  message?: string,
) {
  await trigger.click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  if (message) await expect(dialog).toContainText(message);
  await dialog.getByRole('button', { name: confirmLabel, exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function connectFlow(source: Locator, target: Locator) {
  const from = source.locator('.flow-direction-handle.source');
  const to = target.locator('.flow-direction-handle.target');
  await from.dragTo(to);
}

async function connectArtifact(source: Locator, target: Locator) {
  const from = source.locator('.data-port-handle.source').first();
  const to = target.locator('.data-port-handle.target').first();
  await from.dragTo(to);
}

async function dropAsset(page: Page, asset: Locator, canvas: Locator, position: { x: number; y: number }) {
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  const event = {
    dataTransfer,
    clientX: (box?.x ?? 0) + position.x,
    clientY: (box?.y ?? 0) + position.y,
  };
  await asset.dispatchEvent('dragstart', event);
  await canvas.dispatchEvent('dragover', event);
  await canvas.dispatchEvent('drop', event);
  await dataTransfer.dispose();
}

test('terminal environment creation keeps the setup image internal', async ({ page }) => {
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/v1/terminal-environments', async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: '00000000-0000-0000-0000-000000000013',
          name: submitted.name,
          description: submitted.description,
          row_version: 1,
          versions: [],
          active_sessions: [],
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }),
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await login(page);
  await page.getByRole('button', { name: '终端环境' }).click();
  await page.getByRole('button', { name: '新建环境' }).click();
  const editor = page.locator('form.environment-create-dialog');
  await expect(editor.getByText('基础镜像', { exact: true })).toHaveCount(0);
  await editor.getByLabel('名称').fill('UI 内部启动镜像验收');
  await editor.getByLabel('说明').fill('用户只定义环境元数据');
  await editor.getByRole('button', { name: '创建环境' }).click();
  await expect.poll(() => submitted).toEqual({
    name: 'UI 内部启动镜像验收',
    description: '用户只定义环境元数据',
  });
});

test('environment publishing reopens as progress instead of reconnecting the terminal', async ({ page }) => {
  let terminalAttachments = 0;
  const environment = {
    id: 'environment-publishing',
    name: '发布中的终端环境',
    description: '镜像正在构建',
    row_version: 1,
    versions: [{
      id: 'version-publishing', environment_id: 'environment-publishing', version_no: 1,
      parent_version_id: null, state: 'PUBLISHING', image_reference: '', image_digest: '',
      base_image_reference: 'flowweave-openhands-runtime:1', base_image_digest: 'sha256:seed',
      manifest: {}, error_detail: null, runtime_compatible: false, runtime_incompatibility_reason: null,
      run_reference_count: 0, reference_count: 0, created_at: new Date().toISOString(),
    }],
    active_sessions: [{
      id: 'setup-session-publishing', environment_id: 'environment-publishing', base_version_id: null,
      state: 'PUBLISHING', base_image_reference: 'flowweave-openhands-runtime:1',
      expires_at: new Date(Date.now() + 60_000).toISOString(), error_detail: null,
    }],
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  };
  await page.route('**/api/v1/terminal-environments**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'GET' && pathname.endsWith('/terminal-environments')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([environment]) });
      return;
    }
    await route.fallback();
  });
  await page.routeWebSocket('**/api/v1/environment-setup-sessions/*/terminal*', () => { terminalAttachments += 1; });

  await login(page);
  await page.getByRole('button', { name: '终端环境' }).click();
  await page.getByRole('button', { name: '查看发布进度' }).click();
  await expect(page.getByText('环境版本正在后台发布', { exact: true })).toBeVisible();
  await expect(page.getByText('完成前不能重新连接或停止此终端。', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '关闭视图' }).click();
  await page.getByRole('button', { name: '查看发布进度' }).click();
  await expect(page.getByText('环境版本正在后台发布', { exact: true })).toBeVisible();
  expect(terminalAttachments).toBe(0);
});

test('closing an environment terminal only hides its existing connection', async ({ page }) => {
  let terminalAttachments = 0;
  const terminalInputs: string[] = [];
  const environment = {
    id: 'environment-running', name: '持续连接终端环境', description: '', row_version: 1, versions: [],
    active_sessions: [{
      id: 'setup-session-running', environment_id: 'environment-running', base_version_id: null,
      state: 'RUNNING', base_image_reference: 'flowweave-openhands-runtime:1',
      expires_at: new Date(Date.now() + 60_000).toISOString(), error_detail: null,
    }],
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  };
  await page.route('**/api/v1/terminal-environments**', async route => {
    const request = route.request();
    if (request.method() === 'GET' && new URL(request.url()).pathname.endsWith('/terminal-environments')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([environment]) });
      return;
    }
    await route.fallback();
  });
  await page.routeWebSocket('**/api/v1/environment-setup-sessions/*/terminal*', socket => {
    terminalAttachments += 1;
    socket.send('connected\\r\\n$ ');
    socket.send('\u001b[?1000h\u001b[?1006h');
    socket.onMessage(message => {
      const payload = JSON.parse(message.toString()) as { type?: string; data?: string };
      if (payload.type === 'input' && payload.data) terminalInputs.push(payload.data);
    });
  });

  await login(page);
  await page.getByRole('button', { name: '终端环境' }).click();
  await page.getByRole('button', { name: '继续配置' }).click();
  await expect.poll(() => terminalAttachments).toBe(1);
  const terminalScreen = page.locator('.terminal-screen .xterm-screen');
  const screenBox = await terminalScreen.boundingBox();
  if (!screenBox) throw new Error('Expected environment terminal screen');
  await page.mouse.move(screenBox.x + 18, screenBox.y + 12);
  await page.mouse.down();
  await page.mouse.move(screenBox.x + 92, screenBox.y + 12, { steps: 5 });
  await page.mouse.up();
  expect(terminalInputs.some(data => data.startsWith('\u001b[<'))).toBe(false);
  const copiedTerminalSelection = await terminalScreen.evaluate(screen => {
    const terminal = screen.closest('.xterm');
    if (!terminal) throw new Error('Expected xterm root');
    const event = new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() });
    terminal.dispatchEvent(event);
    return { copied: event.clipboardData?.getData('text/plain'), prevented: event.defaultPrevented };
  });
  expect(copiedTerminalSelection.copied).toContain('nnected');
  expect(copiedTerminalSelection.prevented).toBe(true);
  await page.getByRole('button', { name: '关闭视图' }).click();
  await page.getByRole('button', { name: '继续配置' }).click();
  await expect.poll(() => terminalAttachments).toBe(1);
});

test('terminal environment delete stops active setup sessions before retrying cleanup', async ({ page }) => {
  let environmentVisible = true;
  let environmentDeleteAttempts = 0;
  const stoppedSessions: string[] = [];
  const environment = {
    id: 'environment-with-active-setup',
    name: '待删除 E2E 终端环境',
    description: '删除时应自动停止配置会话',
    row_version: 1,
    versions: [],
    active_sessions: [{
      id: 'setup-session-running',
      environment_id: 'environment-with-active-setup',
      base_version_id: null,
      state: 'RUNNING',
      base_image_reference: 'flowweave-openhands-runtime:1',
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      error_detail: null,
    }],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  const error = (code: string, message: string, details: Record<string, unknown> = {}) => ({
    error: { code, message, details },
  });

  await page.route('**/api/v1/environment-setup-sessions/*', async route => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    stoppedSessions.push(route.request().url().split('/').at(-1) ?? '');
    await route.fulfill({ status: 204 });
  });
  await page.route('**/api/v1/terminal-environments**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'GET' && pathname.endsWith('/terminal-environments')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(environmentVisible ? [environment] : []),
      });
      return;
    }
    if (request.method() === 'DELETE' && pathname.endsWith(`/terminal-environments/${environment.id}`)) {
      environmentDeleteAttempts += 1;
      if (environmentDeleteAttempts === 1) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify(error('ENVIRONMENT_SETUP_ACTIVE', 'Setup container cleanup is pending', {
            session_ids: [environment.active_sessions[0].id],
          })),
        });
        return;
      }
      environmentVisible = false;
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fallback();
  });

  await login(page);
  await page.getByRole('button', { name: '终端环境' }).click();
  const deleteButton = page.getByRole('button', { name: `删除环境 ${environment.name}` });
  await expect(deleteButton).toBeVisible();
  await confirmProductDialog(page, deleteButton, '删除环境', '配置会话会停止并丢弃');

  await expect.poll(() => stoppedSessions).toEqual([environment.active_sessions[0].id]);
  await expect.poll(() => environmentDeleteAttempts).toBe(2);
  await expect(page.getByText(environment.name)).toHaveCount(0);
});

test('terminal environment deletion preserves setup sessions when a FlowRun uses its image', async ({ page }) => {
  const stoppedSessions: string[] = [];
  let environmentDeleteAttempts = 0;
  const environment = {
    id: 'environment-in-use-by-run', name: '被流程运行占用的环境', description: '', row_version: 1, versions: [],
    active_sessions: [{
      id: 'setup-session-to-preserve', environment_id: 'environment-in-use-by-run', base_version_id: null, state: 'RUNNING',
      base_image_reference: 'flowweave-openhands-runtime:1', expires_at: new Date(Date.now() + 60_000).toISOString(), error_detail: null,
    }],
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  };
  await page.route('**/api/v1/environment-setup-sessions/*', async route => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    stoppedSessions.push(route.request().url().split('/').at(-1) ?? '');
    await route.fulfill({ status: 204 });
  });
  await page.route('**/api/v1/terminal-environments**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'GET' && pathname.endsWith('/terminal-environments')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([environment]) });
      return;
    }
    if (request.method() === 'DELETE' && pathname.endsWith(`/terminal-environments/${environment.id}`)) {
      environmentDeleteAttempts += 1;
      await route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ error: {
          code: 'ENVIRONMENT_IN_USE', message: 'The terminal environment is referenced by a Snapshot or FlowRun',
          details: { flow_run_reference_count: 2, snapshot_reference_count: 3 },
        } }),
      });
      return;
    }
    await route.fallback();
  });

  await login(page);
  await page.getByRole('button', { name: '终端环境' }).click();
  const deleteButton = page.getByRole('button', { name: `删除环境 ${environment.name}` });
  await deleteButton.click();
  await page.getByRole('alertdialog').getByRole('button', { name: '删除环境', exact: true }).click();
  const blocked = page.locator('.environments-page > .error');
  await expect(blocked).toContainText('无法删除终端环境');
  await expect(blocked).toContainText('2 个流程运行');
  await expect(blocked).toContainText('3 份冻结快照');
  await expect(blocked).toContainText('配置终端不会被停止');
  await expect.poll(() => environmentDeleteAttempts).toBe(1);
  expect(stoppedSessions).toEqual([]);
});

test('top-level Agent workspace creates a direct conversation and restores its URL', async ({ page }) => {
  let modelIsResponding = false;
  let interrupted = false;
  let agentStream: WebSocketRoute | undefined;
  let terminalSocket: WebSocketRoute | undefined;
  const terminalInputs: string[] = [];
  const terminalResizes: Array<{ rows: number; columns: number }> = [];
  let sentMessages = 0;
  let sentProvider: string | null = null;
  let sentBinding: string | null = null;
  let streamingMigrations = 0;
  let streamingMigrationPayload: Record<string, unknown> | null = null;
  let persistedModelSelection: Record<string, unknown> | null = null;
  let confirmationPending = false;
  let confirmationDecision: Record<string, unknown> | null = null;
  let bootstrapRequests = 0;
  let bootstrapWorkDirectory: string | null = null;
  let bootstrapConversationId: string | null = null;
  let bootstrapIdempotencyKey: string | null = null;
  const bootstrapIdempotencyKeys: Array<string | null> = [];
  let releaseFirstBootstrap: (() => void) | undefined;
  const firstBootstrapGate = new Promise<void>(resolve => { releaseFirstBootstrap = resolve; });
  let renameRequests = 0;
  let contextAvailable = false;
  let manualCondensations = 0;
  let compactionScenario = false;
  const longFinalReply = Array.from(
    { length: 90 },
    (_, index) => `最终回复第 ${index + 1} 段：这是用于验证长回复从开头开始阅读的正式内容。`,
  ).join('\n\n');
  const conversations: Array<Record<string, unknown>> = [];
  await page.routeWebSocket('**/agent-workspaces/**/stream', stream => { agentStream = stream; });
  await page.routeWebSocket('**/agent-workspaces/**/terminal*', socket => {
    terminalSocket = socket;
    socket.onMessage(message => {
      try {
        const payload = JSON.parse(String(message)) as { type?: string; data?: string; rows?: number; columns?: number };
        if (payload.type === 'input' && payload.data) terminalInputs.push(payload.data);
        if (payload.type === 'resize' && Number.isFinite(payload.rows) && Number.isFinite(payload.columns)) {
          terminalResizes.push({ rows: payload.rows!, columns: payload.columns! });
        }
      } catch { /* resize/input frames are asserted through the collected input list. */ }
    });
  });
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const workspace = {
      id: 'agent-workspace-1', display_name: 'Agent 工作区',
      desired_state: 'RUNNING', updated_at: new Date().toISOString(),
    };
    if (path.endsWith('/default')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(workspace) });
      return;
    }
    if (path.endsWith('/runtime')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, message: null, updated_at: new Date().toISOString() }) });
      return;
    }
    if (path.endsWith('/work-directories')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' },
        items: [{
          id: 'directory-backend', display_name: '后端服务', state: 'ACTIVE',
          current_version: { id: 'directory-backend-v1', version: 1, selected_paths: ['backend'], working_directory: '/runtime/workspace/project/backend' },
        }],
      }) });
      return;
    }
    if (path.endsWith('/workspace')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: '/runtime/workspace/project',
        scope: { kind: 'ROOT', display_name: '根工作区' },
        working_directory: '/runtime/workspace/project',
        work_directory: null,
        files: [
          { path: '/runtime/workspace/project/README.md', kind: 'file', size: 128 },
          { path: '/runtime/workspace/project/backend', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/src', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/src/config.ts', kind: 'file', size: 42 },
        ],
        repositories: [{ path: '/runtime/workspace/project', remote: 'https://example.test/repo.git', branch: 'main', head: '1234567890ab' }],
        runtime: { container_id: '2fae71c74c89' },
        ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '需要部署 Gateway', note: '部署方配置受保护入口后可连接。' } },
      }) });
      return;
    }
    if (path.endsWith('/workspace/file')) {
      await route.fulfill({ status: 200, contentType: 'text/plain', body: 'workspace file preview\n' });
      return;
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(conversations) });
      return;
    }
    if (path.endsWith('/conversations') && request.method() === 'POST') {
      bootstrapRequests += 1;
      const payload = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      const modelProviderId = payload.model_provider_id;
      bootstrapWorkDirectory = typeof payload.work_directory_id === 'string' ? payload.work_directory_id : null;
      bootstrapConversationId = typeof payload.conversation_id === 'string' ? payload.conversation_id : null;
      bootstrapIdempotencyKey = await request.headerValue('Idempotency-Key');
      bootstrapIdempotencyKeys.push(bootstrapIdempotencyKey);
      if (bootstrapRequests === 1) {
        // Keep the first create request in flight until the test has observed
        // the draft in the conversation rail.
        await firstBootstrapGate;
        await route.fulfill({ status: 504, contentType: 'application/json', body: JSON.stringify({
          error: { code: 'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS', message: '首条消息正在安全对账，请稍后重试；系统不会重复发送' },
        }) });
        return;
      }
      const created = {
        id: 'agent-conversation-1', display_title: '检查工作目录', title_state: 'PENDING', lifecycle: 'ACTIVE',
        model_provider_id: modelProviderId,
        model_name: 'gpt-test', reasoning_effort: null,
        streaming_callback_ready: true,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_connected_at: null,
      };
      conversations.splice(0, 0, created);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ conversation: created, accepted: true, cursor: 'user-request' }) });
      return;
    }
    if (/\/conversations\/[^/]+$/.test(path) && request.method() === 'PATCH') {
      renameRequests += 1;
      const bindingId = path.match(/\/conversations\/([^/]+)$/)?.[1];
      const binding = conversations.find(item => item.id === bindingId);
      const payload = JSON.parse(request.postData() ?? '{}') as { title: string };
      if (binding) Object.assign(binding, { display_title: payload.title, title_state: 'MANUAL' });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(binding) });
      return;
    }
    if (path.endsWith('/condense') && request.method() === 'POST') {
      manualCondensations += 1;
      await new Promise(resolve => setTimeout(resolve, 1_000));
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ accepted: true }) });
      return;
    }
    if (path.endsWith('/fork') && request.method() === 'POST') {
      const created = {
        id: 'agent-conversation-fork-1', display_title: 'Fork · 检查工作目录', lifecycle: 'ACTIVE',
        model_provider_id: 'provider-1',
        model_name: 'gpt-test', reasoning_effort: null,
        streaming_callback_ready: false,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_connected_at: null,
      };
      conversations.splice(0, 0, created);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(created) });
      return;
    }
    if (path.endsWith('/streaming-migration') && request.method() === 'POST') {
      streamingMigrations += 1;
      streamingMigrationPayload = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      const created = {
        id: 'agent-conversation-streaming-1', display_title: 'Fork · 检查工作目录', lifecycle: 'ACTIVE',
        model_provider_id: streamingMigrationPayload.model_provider_id,
        model_name: streamingMigrationPayload.model_name,
        reasoning_effort: streamingMigrationPayload.reasoning_effort,
        streaming_callback_ready: true,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_connected_at: null,
      };
      conversations.splice(0, 0, created);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(created) });
      return;
    }
    if (path.endsWith('/pending-confirmation') && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(confirmationPending ? {
          pending: true,
          pending_actions_digest: 'batch-digest',
          cursor: 'tool-request',
          actions: [{
            action_id: 'tool-request', tool_call_id: 'tool-call', tool_name: 'terminal',
            arguments: { command: 'pwd' }, security_risk: 'LOW', summary: '查看工作目录', digest: 'action-digest',
          }],
        } : { pending: false }),
      });
      return;
    }
    if (path.endsWith('/pending-confirmation/decision') && request.method() === 'POST') {
      confirmationDecision = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      confirmationPending = false;
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ accepted: true, cursor: 'tool-request' }) });
      return;
    }
    if (path.endsWith('/context')) {
      const forkContext = path.includes('/conversations/agent-conversation-fork-1/');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(forkContext ? {
        used_tokens: null, window_tokens: 922_000, cumulative_tokens: 0,
        model_name: 'gpt-test', reasoning_effort: 'high', usage_current: true,
        proactive_compaction_ratio: 0.8, proactive_compaction_tokens: 737_600, compaction_policy_current: false,
        condenser_max_size: 240,
      } : manualCondensations ? {
        used_tokens: null, window_tokens: 922_000, cumulative_tokens: 12_716,
        model_name: 'gpt-test', reasoning_effort: 'high', usage_current: false,
        proactive_compaction_ratio: 0.8, proactive_compaction_tokens: 737_600, compaction_policy_current: true,
        condenser_max_size: 10_000,
      } : contextAvailable ? {
        used_tokens: 6_380, window_tokens: 922_000, cumulative_tokens: 12_716,
        model_name: 'gpt-test', reasoning_effort: 'high', usage_current: true,
        proactive_compaction_ratio: 0.8, proactive_compaction_tokens: 737_600, compaction_policy_current: true,
        condenser_max_size: 10_000,
      } : {
        used_tokens: 0, window_tokens: 922_000, cumulative_tokens: 12_716,
        model_name: 'gpt-test', reasoning_effort: 'high',
      }) });
      return;
    }
    if (path.endsWith('/model') && request.method() === 'POST') {
      persistedModelSelection = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      const bindingId = path.match(/\/conversations\/([^/]+)\/model$/)?.[1];
      const binding = conversations.find(item => item.id === bindingId);
      if (binding) Object.assign(binding, persistedModelSelection, { updated_at: new Date().toISOString() });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(persistedModelSelection) });
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        events: modelIsResponding ? [{ id: 'running-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'agent-reply', content: '正在处理的请求', timestamp: new Date(Date.now() - 12_000).toISOString().replace(/Z$/, '') } }] : conversations.length ? [
          { id: 'user-request', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '检查工作目录', timestamp: '2026-08-26T10:00:00Z' } },
          { id: 'tool-request', event_type: 'TOOL_CALL', payload: { parent_id: 'user-request', action_id: 'tool-request', tool_call_id: 'terminal-call', tool_name: 'terminal', event_name: 'TerminalAction', content: '我先检查当前工作目录。', thought: '我先检查当前工作目录。', summary: '检查当前工作目录', details: { command: 'pwd' }, timestamp: '2026-08-26T10:00:02Z' } },
          { id: 'tool-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'unrelated-file-action', action_id: 'tool-request', tool_call_id: 'terminal-call', tool_name: 'terminal', event_name: 'TerminalObservation', content: '/workspace', details: { command: 'pwd', exit_code: 0, is_error: false }, timestamp: '2026-08-26T10:00:03Z' } },
          { id: 'file-action', event_type: 'TOOL_CALL', payload: { parent_id: 'tool-result', action_id: 'file-action', tool_call_id: 'file-call', tool_name: 'file_editor', event_name: 'FileEditorAction', summary: '更新运行配置', details: { command: 'str_replace', path: '/runtime/workspace/project/src/config.ts', old_str: 'const mode = "old"', new_str: 'const mode = "new"' }, timestamp: '2026-08-26T10:00:03.200Z' } },
          { id: 'file-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'file-action', action_id: 'file-action', tool_call_id: 'file-call', tool_name: 'file_editor', event_name: 'FileEditorObservation', content: 'The file was edited successfully.', details: { command: 'str_replace', path: '/runtime/workspace/project/src/config.ts', is_error: false }, timestamp: '2026-08-26T10:00:03.500Z' } },
          { id: 'state-empty', event_type: 'STATE', payload: { parent_id: 'file-result', timestamp: '2026-08-26T10:00:04Z' } },
          { id: 'agent-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'state-empty', content: '工作区已就绪。', timestamp: '2026-08-26T10:02:19Z' } },
          { id: 'direct-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'agent-reply', content: '直接回答 https://input.example.test/brief', attachments: [{ filename: '需求截图.png', mime_type: 'image/png', byte_size: 128, path: '/runtime/workspace/project/uploads/source-image.png', image_data_url: 'data:image/png;base64,iVBORw==' }], timestamp: '2026-08-26T10:03:00Z' } },
          { id: 'direct-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'direct-user', content: '直接回复完成。更多信息见 www.output.example.test/result', timestamp: '2026-08-26T10:03:02Z' } },
          { id: 'finish-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'direct-reply', content: '整理任务', timestamp: '2026-08-26T10:03:10Z' } },
          { id: 'tracker-action', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'finish-user', action_id: 'tracker-action', tool_call_id: 'tracker-call', tool_name: 'task_tracker', event_name: 'TaskTrackerAction', content: '我先把执行步骤整理成任务列表。', thought: '我先把执行步骤整理成任务列表。', summary: '整理并更新执行步骤', details: { command: 'plan', task_list: [{ title: '检查构建', status: 'in_progress' }] }, timestamp: '2026-08-26T10:03:11Z' } },
          { id: 'tracker-result', event_type: 'TOOL_RESULT', payload: { source: 'environment', parent_id: 'tracker-action', action_id: 'tracker-action', tool_call_id: 'tracker-call', tool_name: 'task_tracker', event_name: 'TaskTrackerObservation', details: { command: 'plan' }, timestamp: '2026-08-26T10:03:12Z' } },
          { id: 'finish-action', event_type: 'COMPLETED', payload: { source: 'agent', parent_id: 'tracker-result', event_name: 'FinishAction', content: '任务跟踪已完成。', timestamp: '2026-08-26T10:03:13Z' } },
          { id: 'finish-observation', event_type: 'TOOL_RESULT', payload: { source: 'environment', parent_id: 'finish-action', event_name: 'FinishObservation', content: '', timestamp: '2026-08-26T10:03:14Z' } },
          { id: 'failure-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'finish-observation', content: '触发失败', timestamp: '2026-08-26T10:04:00Z' } },
          { id: 'failure-event', event_type: 'ERROR', payload: { source: 'environment', parent_id: 'failure-user', content: 'upstream connection refused', error_code: 'LLMServiceUnavailableError', timestamp: '2026-08-26T10:04:03Z' } },
          ...(compactionScenario ? [
            { id: 'compaction-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'failure-event', content: '完成压缩后继续检查', timestamp: '2026-08-26T10:10:00Z' } },
            { id: 'before-compaction', event_type: 'THOUGHT', payload: { source: 'agent', parent_id: 'compaction-user', content: '先整理当前信息。', timestamp: '2026-08-26T10:10:02Z' } },
            { id: 'automatic-condensation-request', event_type: 'CONDENSATION_REQUESTED', payload: { source: 'agent', parent_id: 'before-compaction', condensation_reason_detail: 'Token 已达到 80% 主动压缩阈值。', timestamp: '2026-08-26T10:10:10Z' } },
            { id: 'automatic-condensation-completed', event_type: 'CONDENSATION_COMPLETED', payload: { source: 'agent', parent_id: 'automatic-condensation-request', condensation_request_event_id: 'automatic-condensation-request', forgotten_event_ids: ['before-compaction'], condensation_reason_detail: 'Token 已达到 80% 主动压缩阈值。', condensation_triggered_at: '2026-08-26T10:10:10Z', condensation_completed_at: '2026-08-26T10:10:12Z', timestamp: '2026-08-26T10:10:12Z' } },
            { id: 'after-compaction-tool', event_type: 'TOOL_CALL', payload: { source: 'agent', parent_id: 'automatic-condensation-completed', action_id: 'after-compaction-tool', tool_call_id: 'after-compaction-call', event_name: 'TerminalAction', details: { command: 'git status --short' }, timestamp: '2026-08-26T10:10:14Z' } },
            { id: 'after-compaction-result', event_type: 'TOOL_RESULT', payload: { source: 'environment', parent_id: 'after-compaction-tool', action_id: 'after-compaction-tool', tool_call_id: 'after-compaction-call', event_name: 'TerminalObservation', content: 'clean', details: { command: 'git status --short', exit_code: 0 }, timestamp: '2026-08-26T10:10:16Z' } },
            { id: 'after-compaction-reply', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'after-compaction-result', content: '压缩后检查完成。', timestamp: '2026-08-26T10:10:20Z' } },
          ] : []),
          ...(manualCondensations && !compactionScenario ? [{ id: 'manual-condensation', event_type: 'CONDENSATION_COMPLETED', payload: { source: 'agent', parent_id: 'failure-event', event_name: 'Condensation', summary: '已压缩较早上下文', forgotten_event_ids: ['tool-request', 'tool-result'], condensation_reason: 'REQUEST', condensation_reason_detail: 'OpenHands 收到显式压缩请求；该请求可能来自手动压缩、上下文用量主动保护或模型上下文超限后的恢复。', condensation_triggered_at: '2026-08-26T10:04:58Z', condensation_completed_at: '2026-08-26T10:05:00Z', timestamp: '2026-08-26T10:05:00Z' } }] : []),
        ] : [], next_cursor: null,
      }) });
      return;
    }
    if (path.endsWith('/messages') && request.method() === 'POST') {
      sentProvider = JSON.parse(request.postData() ?? '{}').model_provider_id ?? null;
      sentBinding = path.match(/\/conversations\/([^/]+)\/messages$/)?.[1] ?? null;
      sentMessages += 1;
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ accepted: true, cursor: sentMessages === 1 ? 'running-user' : `sent-user-${sentMessages}` }) });
      return;
    }
    if (path.endsWith('/interrupt') && request.method() === 'POST') {
      interrupted = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ accepted: true }) });
      return;
    }
    if (path.endsWith('/input-readiness')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        ready: !modelIsResponding || interrupted,
        execution_status: modelIsResponding ? (interrupted ? 'paused' : 'running') : 'idle',
      }) });
      return;
    }
    if (path.endsWith('/resume') && request.method() === 'POST') {
      confirmationPending = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ accepted: true, cursor: 'running-user' }) });
      return;
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });

  await page.route('**/api/v1/model-providers', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{
      id: 'provider-1', name: '已测试模型', connection_state: 'CONNECTED', models: [{ model_name: 'gpt-test', enabled: true, is_default: true }],
    }, {
      id: 'provider-2', name: '另一模型配置', connection_state: 'CONNECTED', models: [{ model_name: 'gpt-second', enabled: true, is_default: true, default_reasoning_effort: 'high', supported_reasoning_efforts: ['low', 'high'] }],
    }]),
  }));
  await login(page);
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/agent');
  await expect(page.getByRole('button', { name: '新建会话' }).first()).toBeEnabled();
  await expect(page.getByText('后端服务', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '在后端服务中新建会话' }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(page.locator('.agent-composer-model-summary')).toHaveText('gpt-test');
  expect(bootstrapRequests).toBe(0);
  await page.getByRole('button', { name: '新建会话' }).first().click();
  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.getByText('会话已就绪', { exact: true })).toBeVisible();
  expect(bootstrapRequests).toBe(0);
  await expect(page.getByRole('button', { name: '检查工作目录' })).toHaveCount(0);
  await page.getByLabel('发送 Agent 消息').fill('检查工作目录');
  await page.getByLabel('发送消息').click();
  const pendingConversation = page.locator('.agent-workbench-list').getByRole('button', { name: '检查工作目录，正在创建会话' });
  await expect(pendingConversation).toBeVisible();
  releaseFirstBootstrap?.();
  await expect(page.getByText('正在安全核对首条消息', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/\/agent\/conversations\/agent-conversation-1$/);
  await expect.poll(() => bootstrapRequests).toBe(2);
  expect(bootstrapWorkDirectory).toBeNull();
  expect(bootstrapIdempotencyKey).toBe(bootstrapConversationId);
  expect(bootstrapIdempotencyKeys).toHaveLength(2);
  expect(bootstrapIdempotencyKeys[0]).toBe(bootstrapIdempotencyKeys[1]);
  await expect(page.getByRole('heading', { name: '检查工作目录' })).toBeVisible();
  await expect(page.locator('.agent-workbench-header').getByLabel(/工作区工具/)).toHaveCount(0);
  await expect(page.locator('.agent-workspace-summary').getByLabel('打开工作区工具')).toBeVisible();
  await page.getByRole('heading', { name: '检查工作目录' }).dblclick();
  const titleEditor = page.getByLabel('会话标题');
  await expect(titleEditor).toHaveValue('检查工作目录');
  expect(await titleEditor.evaluate(element => ({
    start: (element as HTMLInputElement).selectionStart,
    end: (element as HTMLInputElement).selectionEnd,
  }))).toEqual({ start: 0, end: '检查工作目录'.length });
  await titleEditor.fill('不应保存的标题');
  await titleEditor.press('Escape');
  await expect(page.getByRole('heading', { name: '检查工作目录' })).toBeVisible();
  expect(renameRequests).toBe(0);
  await page.getByRole('heading', { name: '检查工作目录' }).dblclick();
  await page.getByLabel('会话标题').fill('   ');
  await page.getByLabel('会话标题').blur();
  await expect(page.getByRole('heading', { name: '检查工作目录' })).toBeVisible();
  expect(renameRequests).toBe(0);
  await page.getByRole('heading', { name: '检查工作目录' }).dblclick();
  await page.getByLabel('会话标题').press('Enter');
  await expect.poll(() => renameRequests).toBe(1);
  await expect(page.getByRole('heading', { name: '检查工作目录' })).toBeVisible();
  const compactConversationItem = page.getByRole('button', { name: /检查工作目录/ }).first();
  await expect.poll(() => compactConversationItem.evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(40);
  await expect(page.getByText('工作区已就绪。')).toBeVisible();
  await expect(page.getByText('需要部署 Gateway', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '文件', exact: true }).click();
  await expect(page.getByText('README.md', { exact: true })).toBeVisible();
  await page.getByText('README.md', { exact: true }).click();
  await expect(page.getByText('workspace file preview', { exact: true })).toBeVisible();
  await page.getByLabel('新增工作区工具').click();
  await page.getByRole('button', { name: '终端', exact: true }).click();
  await expect(page.locator('.agent-workspace-terminal')).toBeVisible();
  await expect.poll(() => Boolean(terminalSocket)).toBe(true);
  terminalSocket!.send(`${Array.from({ length: 80 }, (_, index) => `output-${index + 1}`).join('\r\n')}\r\n`);
  const viewport = page.locator('.agent-workspace-terminal .xterm-viewport');
  const terminalLayout = await page.getByLabel('Agent 工作区终端').evaluate(host => {
    const drawer = host.closest('.agent-workspace-drawer');
    const screen = host.querySelector('.xterm-screen');
    if (!drawer || !screen) throw new Error('Expected terminal drawer and screen');
    const hostBox = host.getBoundingClientRect();
    const drawerBox = drawer.getBoundingClientRect();
    const screenBox = screen.getBoundingClientRect();
    const hostStyle = getComputedStyle(host);
    return {
      hostBottom: hostBox.bottom,
      drawerBottom: drawerBox.bottom,
      screenBottom: screenBox.bottom,
      hostRight: hostBox.right,
      drawerRight: drawerBox.right,
      screenRight: screenBox.right,
      hostHeight: hostBox.height,
      drawerHeight: drawerBox.height,
      hostContentBottom: hostBox.bottom - parseFloat(hostStyle.paddingBottom),
      hostContentRight: hostBox.right - parseFloat(hostStyle.paddingRight),
    };
  });
  expect(terminalLayout.hostBottom).toBeLessThanOrEqual(terminalLayout.drawerBottom + 1);
  expect(terminalLayout.screenBottom).toBeLessThanOrEqual(terminalLayout.hostContentBottom + 1);
  expect(terminalLayout.hostHeight).toBeGreaterThan(terminalLayout.drawerHeight - 110);
  expect(terminalLayout.hostRight).toBeLessThanOrEqual(terminalLayout.drawerRight + 1);
  expect(terminalLayout.screenRight).toBeLessThanOrEqual(terminalLayout.hostContentRight + 1);
  await expect.poll(() => terminalResizes.length).toBeGreaterThan(0);
  const initialColumns = terminalResizes.at(-1)!.columns;
  const initialDrawerWidth = await page.locator('.agent-workspace-drawer').evaluate(drawer => drawer.getBoundingClientRect().width);
  const resizer = page.getByRole('separator', { name: '调整工作区工具宽度' });
  const resizerBox = await resizer.boundingBox();
  if (!resizerBox) throw new Error('Expected workspace drawer resizer');
  await page.mouse.move(resizerBox.x + resizerBox.width / 2, resizerBox.y + 80);
  await page.mouse.down();
  await page.mouse.move(Math.max(2, resizerBox.x - 500), resizerBox.y + 80, { steps: 12 });
  await page.mouse.up();
  await expect.poll(() => page.locator('.agent-workspace-drawer').evaluate(drawer => drawer.getBoundingClientRect().width)).toBeGreaterThan(initialDrawerWidth + 150);
  await expect.poll(() => terminalResizes.at(-1)?.columns ?? 0).toBeGreaterThan(initialColumns);
  const expandedLayout = await page.getByLabel('Agent 工作区终端').evaluate(host => {
    const screen = host.querySelector('.xterm-screen');
    const viewport = host.querySelector('.xterm-viewport');
    if (!screen || !viewport) throw new Error('Expected terminal screen and viewport');
    const hostBox = host.getBoundingClientRect();
    const screenBox = screen.getBoundingClientRect();
    const viewportBox = viewport.getBoundingClientRect();
    const hostStyle = getComputedStyle(host);
    const contentRight = hostBox.right - parseFloat(hostStyle.paddingRight);
    return {
      screenRight: screenBox.right,
      viewportRight: viewportBox.right,
      contentRight,
      horizontalOverflow: host.scrollWidth - host.clientWidth,
    };
  });
  expect(expandedLayout.screenRight).toBeLessThanOrEqual(expandedLayout.contentRight + 1);
  expect(expandedLayout.viewportRight).toBeLessThanOrEqual(expandedLayout.contentRight + 1);
  expect(expandedLayout.horizontalOverflow).toBeLessThanOrEqual(1);
  await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop < 3)).toBe(true);
  terminalSocket!.send('\u001b[?1000h\u001b[?1006h');
  await viewport.dispatchEvent('wheel', { deltaY: -120, deltaMode: 0 });
  await expect.poll(() => terminalInputs.some(data => data.startsWith('\u001b[<64;') && data.endsWith('M'))).toBe(true);
  expect(terminalInputs.some(data => data.includes('\u001b[A') || data.includes('\u001b[B'))).toBe(false);
  const mouseInputsBeforeSelection = terminalInputs.length;
  const terminalScreen = page.locator('.agent-workspace-terminal .xterm-screen');
  const screenBox = await terminalScreen.boundingBox();
  if (!screenBox) throw new Error('Expected terminal screen');
  await page.mouse.move(screenBox.x + 18, screenBox.y + 26);
  await page.mouse.down();
  await page.mouse.move(screenBox.x + 92, screenBox.y + 26, { steps: 5 });
  await page.mouse.up();
  expect(terminalInputs.slice(mouseInputsBeforeSelection).some(data => data.startsWith('\u001b[<'))).toBe(false);
  const copiedTerminalSelection = await terminalScreen.evaluate(screen => {
    const terminal = screen.closest('.xterm');
    if (!terminal) throw new Error('Expected xterm root');
    const event = new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() });
    terminal.dispatchEvent(event);
    return { copied: event.clipboardData?.getData('text/plain'), prevented: event.defaultPrevented };
  });
  expect(copiedTerminalSelection.copied).toMatch(/put-\d+/);
  expect(copiedTerminalSelection.prevented).toBe(true);
  await expect(page.locator('.agent-context-progress.token')).toContainText('Token0 / 922,000');
  await expect(page.locator('.agent-context-progress.activity')).toHaveCount(1);
  await expect(page.getByText('上下文用量正在从 OpenHands 读取')).toHaveCount(0);
  contextAvailable = true;
  await page.reload();
  await expect(page.locator('.agent-context-progress.token')).toContainText('Token6,380 / 922,000');
  await expect(page.locator('.agent-context-progress.token')).toHaveAttribute('title', /OpenHands 当前 View.*80%/);
  await expect(page.locator('.agent-context-progress.activity')).toContainText(/事件\d+ \/ 10,000/);
  await expect(page.locator('.agent-context-progress.activity')).toHaveAttribute('title', /当前活动事件.*10,000/);
  const composerAfterReload = page.getByLabel('发送 Agent 消息');
  await composerAfterReload.fill('/');
  const nativeMenu = page.getByRole('listbox', { name: '选择 OpenHands 原生能力、命令或 MCP' });
  await expect(nativeMenu.getByText('OpenHands 原生能力', { exact: true })).toBeVisible();
  await expect(nativeMenu.getByRole('option', { name: /压缩上下文/ })).toBeVisible();
  await nativeMenu.getByRole('option', { name: /压缩上下文/ }).click();
  const lowUsageConfirmation = page.getByLabel('确认低用量上下文压缩');
  await expect(lowUsageConfirmation).toBeVisible();
  await expect(lowUsageConfirmation).toContainText('Token 6,380 / 922,000（1%）');
  await expect(lowUsageConfirmation).toContainText('仍会调用摘要模型');
  expect(manualCondensations).toBe(0);
  await lowUsageConfirmation.getByRole('button', { name: '取消' }).click();
  await expect(lowUsageConfirmation).toHaveCount(0);
  expect(manualCondensations).toBe(0);
  await composerAfterReload.fill('/');
  await nativeMenu.getByRole('option', { name: /压缩上下文/ }).click();
  await page.getByRole('button', { name: '仍然压缩' }).click();
  const condensationProgress = page.getByLabel('正在压缩上下文');
  await expect(condensationProgress).toBeVisible();
  await expect(condensationProgress).toContainText('已提交原生压缩请求');
  await expect.poll(() => manualCondensations).toBe(1);
  await expect(condensationProgress).toHaveCount(0);
  await expect(page.locator('.agent-context-progress.token')).toContainText('Token待模型更新');
  await expect(page.locator('.agent-context-progress.activity')).toHaveCount(1);
  await expect(page.getByText('压缩已完成，等待下次模型调用更新用量', { exact: true })).toBeVisible();
  const condensationTimeline = page.getByLabel('上下文压缩记录');
  await expect(condensationTimeline.getByText('已触发上下文压缩', { exact: true })).toBeVisible();
  await expect(condensationTimeline.getByText('上下文压缩已完成', { exact: true })).toBeVisible();
  await expect(condensationTimeline).toContainText('OpenHands 收到显式压缩请求');
  await expect(condensationTimeline).toContainText('完整事件记录仍然保留');
  await expect(condensationTimeline.locator('.conversation-condensation-notice')).toHaveCount(2);
  await expect(condensationTimeline.locator('time')).toHaveCount(2);
  await expect(condensationTimeline.locator('.conversation-activity-group')).toHaveCount(0);
  compactionScenario = true;
  await page.reload();
  const compactionTurn = page.locator('.conversation-turn').filter({ hasText: '压缩后检查完成。' });
  const compactionProcesses = compactionTurn.locator('.conversation-activity-group');
  await expect(compactionProcesses).toHaveCount(2);
  await expect(compactionProcesses.nth(0).getByText('耗时 10秒')).toBeVisible();
  await expect(compactionProcesses.nth(1).getByText('耗时 8秒')).toBeVisible();
  await expect(compactionTurn.getByLabel('上下文压缩记录')).toContainText('Token 已达到 80% 主动压缩阈值');
  await expect(compactionTurn.locator('.conversation-condensation-notice').first()).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect.poll(() => compactionTurn.evaluate(turn => [...turn.children].map(block => block.className))).toEqual([
    'conversation-message user',
    'conversation-activity-group',
    'conversation-condensation-timeline',
    'conversation-activity-group',
    'conversation-message assistant',
  ]);
  await expect(compactionProcesses.nth(1)).toContainText('已运行 git status --short');
  compactionScenario = false;
  await page.reload();
  const completedTurn = page.locator('.conversation-turn').filter({ hasText: '工作区已就绪。' });
  const completedProcess = completedTurn.locator('.conversation-activity-group');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await completedTurn.getByRole('button', { name: '复制消息' }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('检查工作目录');
  const crossedUserSelection = await completedTurn.evaluate(turn => {
    const user = turn.querySelector<HTMLElement>('.conversation-message.user .conversation-message-content');
    const reply = turn.querySelector<HTMLElement>('.conversation-message.assistant');
    if (!user || !reply || !user.firstChild) throw new Error('Expected completed turn content');
    const selection = window.getSelection();
    const range = document.createRange();
    range.setStart(user.firstChild, 0);
    range.setEnd(reply, reply.childNodes.length);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const event = new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() });
    document.dispatchEvent(event);
    return { copied: event.clipboardData?.getData('text/plain'), prevented: event.defaultPrevented };
  });
  expect(crossedUserSelection).toEqual({ copied: '检查工作目录', prevented: true });
  const assistantSelection = await completedTurn.evaluate(turn => {
    const reply = turn.querySelector<HTMLElement>('.conversation-message.assistant');
    if (!reply) throw new Error('Expected assistant reply');
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(reply);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const event = new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() });
    document.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(assistantSelection).toBe(false);
  await expect(completedProcess).toHaveJSProperty('open', false);
  await expect(completedProcess.getByText('耗时 2分钟19秒')).toBeVisible();
  await expect(completedTurn).toHaveJSProperty('nodeName', 'SECTION');
  await expect.poll(() => completedTurn.evaluate(turn => {
    const process = turn.querySelector('.conversation-activity-group');
    const reply = turn.querySelector('.conversation-message.assistant');
    return Boolean(process && reply && (process.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING));
  })).toBe(true);
  await expect(page.getByText('工作区已就绪。')).toHaveCount(1);
  await completedProcess.getByText('耗时 2分钟19秒').click();
  const terminalActivity = completedProcess.locator('.conversation-activity-row.tool').filter({ hasText: '已运行 pwd' });
  const terminalDetail = terminalActivity;
  await expect(terminalActivity).toHaveCount(1);
  await expect(terminalDetail).toHaveJSProperty('open', false);
  await terminalDetail.locator(':scope > summary').click();
  await expect(terminalDetail.getByText('$ pwd', { exact: true })).toBeVisible();
  await expect(terminalDetail.getByText('/workspace', { exact: true })).toBeVisible();
  await expect(terminalDetail.getByText('退出码 0', { exact: true })).toBeVisible();
  const fileActivity = completedProcess.locator('.conversation-activity-row.tool').filter({ hasText: '已编辑 工作区/src/config.ts' });
  const fileDetail = fileActivity;
  await expect(fileActivity).toHaveCount(1);
  await expect(fileDetail).toHaveJSProperty('open', false);
  await fileDetail.locator(':scope > summary').click();
  await expect(fileDetail.getByText('const mode = "old"', { exact: true })).toBeVisible();
  await expect(fileDetail.getByText('const mode = "new"', { exact: true })).toBeVisible();
  await expect(fileDetail.getByText('The file was edited successfully.', { exact: true })).toBeVisible();
  await expect(completedProcess.locator('.conversation-activity-row.tool')).toHaveCount(2);
  const messageRuler = page.getByRole('navigation', { name: '用户消息导航' });
  await expect(messageRuler).toBeVisible();
  await expect(page.locator('.message-position-navigator, .message-position-preview')).toHaveCount(0);
  await expect(messageRuler.getByRole('button')).toHaveCount(4);
  await expect.poll(() => messageRuler.getByRole('button').evaluateAll(buttons => {
    const tops = buttons.map(button => button.getBoundingClientRect().top);
    return Math.max(...tops) - Math.min(...tops);
  })).toBeLessThanOrEqual(30);
  const firstMessageTick = messageRuler.getByRole('button', { name: '定位到用户消息：检查工作目录' });
  await firstMessageTick.hover();
  await expect(page.locator('#conversation-message-preview')).toContainText('检查工作目录');
  await expect(firstMessageTick.locator('.conversation-message-index-tick')).toHaveCSS('width', '15px');
  await firstMessageTick.click();
  await expect.poll(() => page.locator('[data-user-event-id="user-request"]').evaluate(message => {
    const surface = message.closest('.conversation-surface');
    if (!surface) throw new Error('Expected conversation surface');
    return message.getBoundingClientRect().top - surface.getBoundingClientRect().top;
  })).toBeGreaterThanOrEqual(-1);
  await expect.poll(() => page.locator('[data-user-event-id="user-request"]').evaluate(message => {
    const surface = message.closest('.conversation-surface');
    if (!surface) throw new Error('Expected conversation surface');
    return message.getBoundingClientRect().top - surface.getBoundingClientRect().top;
  })).toBeLessThan(80);
  await page.getByRole('button', { name: '跳转到最新回复' }).click();
  await expect(firstMessageTick).not.toHaveAttribute('aria-current', 'location');
  const directTurn = page.locator('.conversation-turn').filter({ hasText: '直接回复完成。' });
  const inputLink = directTurn.getByRole('link', { name: 'https://input.example.test/brief' });
  await expect(inputLink).toHaveAttribute('href', 'https://input.example.test/brief');
  await expect(inputLink).toHaveAttribute('target', '_blank');
  await expect(inputLink).toHaveAttribute('rel', 'noopener noreferrer');
  const outputLink = directTurn.getByRole('link', { name: 'www.output.example.test/result' });
  await expect(outputLink).toHaveAttribute('href', 'http://www.output.example.test/result');
  await expect(outputLink).toHaveAttribute('target', '_blank');
  await expect(outputLink).toHaveAttribute('rel', 'noopener noreferrer');
  const sources = page.locator('.agent-workspace-source-list');
  await expect(sources).toContainText('需求截图.png');
  await expect(sources.getByRole('link', { name: 'input.example.test/brief' })).toHaveAttribute('href', 'https://input.example.test/brief');
  await expect(sources.getByRole('link', { name: 'output.example.test/result' })).toHaveCount(0);
  await expect(directTurn.locator('.conversation-activity-group.summary-only').getByText('耗时 2秒')).toBeVisible();
  await expect(directTurn.locator('.conversation-activity-list')).toHaveCount(0);
  const finishTurn = page.locator('.conversation-turn').filter({ hasText: '任务跟踪已完成。' });
  await expect(finishTurn.locator('.conversation-message.assistant')).toHaveCount(1);
  await expect(finishTurn.getByText('耗时 3秒')).toBeVisible();
  await finishTurn.getByText('耗时 3秒').click();
  await expect(finishTurn.getByText('任务列表已更新')).toBeVisible();
  await expect(finishTurn.getByText('任务跟踪 · 已完成', { exact: true })).toBeVisible();
  await expect(finishTurn.getByText('我先把执行步骤整理成任务列表。')).toBeVisible();
  await expect(finishTurn.getByText('任务跟踪已完成。')).toHaveCount(1);
  const failureTurn = page.locator('.conversation-turn').filter({ hasText: '本轮没有生成回复' });
  await expect(failureTurn).toContainText('网络连接异常，模型服务在 5 次尝试后仍未响应');
  await expect(failureTurn).not.toContainText('upstream connection refused');
  await expect(failureTurn.locator('.conversation-turn-status')).toHaveCount(0);
  await expect(failureTurn.locator('.conversation-activity-group').getByText('耗时 3秒')).toBeVisible();
  await expect(failureTurn.locator('.conversation-message.assistant')).toHaveCount(0);
  await expect.poll(() => failureTurn.evaluate(turn => {
    const process = turn.querySelector('.conversation-activity-group');
    const failure = turn.querySelector('.conversation-failure');
    return Boolean(process && failure && (process.compareDocumentPosition(failure) & Node.DOCUMENT_POSITION_FOLLOWING));
  })).toBe(true);
  await page.getByRole('button', { name: '从此处分叉会话' }).last().click();
  await expect(page).toHaveURL(/\/agent\/conversations\/agent-conversation-fork-1$/);
  await expect(page.getByRole('heading', { name: 'Fork · 检查工作目录' })).toBeVisible();
  await expect(page.getByText('耗时 2分钟19秒')).toBeVisible();
  await expect(page.getByText('TerminalAction')).toBeHidden();
  await page.getByText('耗时 2分钟19秒').click();
  await expect(page.getByText('我先检查当前工作目录。')).toBeVisible();
  await expect(page.getByText('已运行 pwd')).toBeVisible();
  await expect(page.getByText('已编辑 工作区/src/config.ts')).toBeVisible();
  await expect(page.getByText('TerminalAction')).toHaveCount(0);
  await expect(page.getByText('STATE')).not.toBeVisible();
  await expect(page.getByText('当前供应商：已测试模型')).toBeVisible();
  await expect(page.getByLabel('历史压缩策略兼容保护')).toBeVisible();
  await expect(page.locator('.agent-context-progress.token')).toContainText('Token0 / 922,000');
  await expect(page.locator('.agent-context-progress.activity')).toContainText(/事件\d+ \/ 240/);
  const forkComposer = page.getByLabel('发送 Agent 消息');
  await expect(forkComposer).toBeEnabled();
  await forkComposer.fill('分叉后可以继续输入');
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled();
  await forkComposer.fill('');
  await expect(page.locator('.agent-composer-model-summary')).toHaveText('gpt-test高');
  await page.getByLabel('打开模型与推理设置').click();
  await expect(page.locator('.agent-composer-model-popover')).toBeVisible();
  await page.getByRole('heading', { name: 'Fork · 检查工作目录' }).click();
  await expect(page.locator('.agent-composer-model-popover')).toBeHidden();
  await page.getByLabel('打开模型与推理设置').click();
  await page.getByRole('button', { name: '供应商 已测试模型' }).click();
  await page.getByLabel('选择供应商').getByRole('button', { name: '另一模型配置' }).click();
  await expect.poll(() => persistedModelSelection).toEqual({
    model_provider_id: 'provider-2', model_name: 'gpt-second', reasoning_effort: 'high',
  });
  await expect(page.locator('.agent-composer-model-summary')).toHaveText('gpt-second高');
  await expect(page.getByText('当前供应商：另一模型配置')).toBeVisible();
  await page.reload();
  await expect(page.locator('.agent-composer-model-summary')).toHaveText('gpt-second高');
  await expect(page.getByText('当前供应商：另一模型配置')).toBeVisible();
  modelIsResponding = true;
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('maven');
  await composer.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  await expect.poll(() => sentMessages).toBe(0);
  await expect(composer).toHaveValue('maven');
  await composer.fill('第一条排队测试消息');
  await expect(page.locator('.agent-composer-actions .agent-send')).toHaveCount(1);
  await composer.press('Enter');
  await expect(page).toHaveURL(/\/agent\/conversations\/agent-conversation-streaming-1$/);
  await expect.poll(() => streamingMigrations).toBe(1);
  await expect.poll(() => streamingMigrationPayload).toEqual({
    model_provider_id: 'provider-2', model_name: 'gpt-second', reasoning_effort: 'high',
  });
  await expect.poll(() => sentBinding).toBe('agent-conversation-streaming-1');
  await expect.poll(() => sentMessages).toBe(1);
  expect(sentProvider).toBeNull();
  // Reloading during a native turn must restore the formal non-ready state,
  // not leave a static zero-second process card behind.
  await page.reload();
  await expect(page.getByText(/已耗时 \d+秒/)).toBeVisible();
  await expect(page.getByText('仍在等待模型响应')).toHaveCount(0);
  const activeProcess = page.locator('.conversation-turn').last().locator('.conversation-activity-group');
  await expect(activeProcess).toHaveClass(/summary-only/);
  await expect(activeProcess.getByText(/已耗时 \d+秒/)).toBeVisible();
  await expect(activeProcess.getByText(/已耗时 .*小时/)).toHaveCount(0);
  await expect(activeProcess.locator('.conversation-response-wait')).toHaveCount(0);
  await expect(page.locator('.conversation-turn-status')).toHaveText(/正在思考/);
  await expect.poll(() => Boolean(agentStream)).toBe(true);
  agentStream!.send(JSON.stringify({ type: 'delta', content: '正在核对上下文。' }));
  await expect(activeProcess.getByText('正在核对上下文。')).toBeVisible();
  await expect(page.locator('.conversation-message.assistant').filter({ hasText: '正在核对上下文。' })).toHaveCount(0);
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: { id: 'live-tool', event_type: 'TOOL_CALL', payload: { parent_id: 'running-user', action_id: 'live-tool', tool_call_id: 'live-call', tool_name: 'terminal', event_name: 'TerminalAction', content: '已完成初步分析。', thought: '已完成初步分析。', summary: '核对项目上下文', details: { command: 'pwd' }, timestamp: new Date().toISOString() } },
  }));
  await expect(activeProcess.getByText('已完成初步分析。')).toBeVisible();
  await expect(activeProcess.getByText('正在运行 pwd')).toBeVisible();
  await expect(page.locator('.conversation-turn-status')).toHaveText(/正在后台执行命令/);
  await expect(activeProcess.getByText('正在核对上下文。')).toHaveCount(0);
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: { id: 'live-tool-result', event_type: 'TOOL_RESULT', payload: { parent_id: 'live-tool', action_id: 'live-tool', tool_call_id: 'live-call', tool_name: 'terminal', event_name: 'TerminalObservation', content: '/runtime/workspace/project', details: { command: 'pwd', exit_code: 0, is_error: false }, timestamp: new Date().toISOString() } },
  }));
  await expect(activeProcess.getByText('已运行 pwd')).toBeVisible();
  await expect(page.locator('.conversation-turn-status')).toHaveText(/正在思考/);
  await expect(activeProcess.locator('.conversation-activity-row.tool')).toHaveCount(1);
  const liveToolDetail = activeProcess.locator('.conversation-tool-detail');
  await expect(liveToolDetail).toHaveJSProperty('open', false);
  await liveToolDetail.locator(':scope > summary').click();
  await expect(liveToolDetail.getByText('/runtime/workspace/project', { exact: true })).toBeVisible();
  await expect(page.locator('.agent-composer-actions .agent-send')).toHaveCount(1);
  await page.getByRole('button', { name: '暂停当前 Agent' }).click();
  await expect(page.getByRole('button', { name: '继续当前 Agent' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: '继续当前 Agent' })).toBeVisible();
  await expect(page.locator('.agent-composer-actions .agent-send')).toHaveCount(1);
  await page.getByRole('button', { name: '继续当前 Agent' }).click();
  await expect(page.getByLabel('工具执行确认')).toBeVisible();
  await expect(page.getByRole('button', { name: '等待工具确认' })).toBeDisabled();
  await expect(page.locator('.agent-composer-actions .agent-send')).toHaveCount(1);
  await expect(page.getByText('查看工作目录')).toBeVisible();
  await page.getByLabel('工具确认理由').fill('测试中拒绝执行');
  await page.getByRole('button', { name: '拒绝整批' }).click();
  await expect.poll(() => confirmationDecision).toEqual({
    expected_pending_digest: 'batch-digest', accept: false, reason: '测试中拒绝执行',
  });
  await expect(page.getByLabel('工具执行确认')).toHaveCount(0);
  await expect(page.locator('.agent-composer-actions .agent-send')).toHaveCount(1);
  await expect(page.getByText('Agent 正在处理上一条消息或停止请求，请稍候')).toHaveCount(0);
  modelIsResponding = false;
  interrupted = false;
  agentStream!.send(JSON.stringify({
    type: 'event',
    event: { id: 'live-finish', event_type: 'COMPLETED', payload: { source: 'agent', parent_id: 'live-tool-result', event_name: 'FinishAction', content: longFinalReply, thought: '核对已经完成，下面给出最终结果。', summary: '整理最终结果', timestamp: new Date().toISOString() } },
  }));
  agentStream!.send(JSON.stringify({ type: 'message_complete' }));
  await expect(page.getByText('核对已经完成，下面给出最终结果。')).toHaveCount(1);
  await expect(page.getByText(/最终回复第 1 段/)).toHaveCount(1);
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
  await expect(activeProcess.getByText('分析中', { exact: true })).toHaveCount(0);
  await expect(activeProcess).toHaveJSProperty('open', false);
  await expect(page.locator('.conversation-turn-status')).toHaveCount(0);
  const completedViewport = await page.locator('.conversation-turn').last().evaluate(turn => {
    const surface = turn.closest('.conversation-surface');
    const reply = turn.querySelector('.conversation-message.assistant');
    if (!surface || !reply) throw new Error('Expected completed response');
    const surfaceBox = surface.getBoundingClientRect();
    const replyBox = reply.getBoundingClientRect();
    return {
      replyTop: replyBox.top,
      replyBottom: replyBox.bottom,
      surfaceTop: surfaceBox.top,
      surfaceBottom: surfaceBox.bottom,
    };
  });
  expect(completedViewport.replyTop).toBeGreaterThanOrEqual(completedViewport.surfaceTop - 1);
  expect(completedViewport.replyTop).toBeLessThan(completedViewport.surfaceTop + 80);
  expect(completedViewport.replyBottom).toBeGreaterThan(completedViewport.surfaceBottom);
  await page.getByRole('button', { name: '跳转到最新回复' }).click();
  await expect.poll(() => page.locator('.conversation-surface').evaluate(surface => surface.scrollHeight - surface.scrollTop - surface.clientHeight)).toBeLessThanOrEqual(16);
  await expect(page.getByRole('button', { name: '跳转到最新回复' })).toHaveCount(0);
  await page.locator('.conversation-turn').last().locator('.conversation-activity-group > summary').click();
  await expect(page.getByText('核对已经完成，下面给出最终结果。')).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/\/agent\/conversations\/agent-conversation-streaming-1$/);
  await expect(page.getByRole('heading', { name: 'Fork · 检查工作目录' })).toBeVisible();
});

test('editing the latest user message locally replaces only its active branch', async ({ page }) => {
  let rewritten = false;
  let releaseRewrite: (() => void) | undefined;
  const events = () => rewritten ? [
    { id: 'earlier-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '更早的问题', timestamp: '2026-08-28T10:00:00Z' } },
    { id: 'earlier-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'earlier-user', content: '更早的回答', timestamp: '2026-08-28T10:00:01Z' } },
    { id: 'rethink-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'earlier-answer', content: '修改后的问题', timestamp: '2026-08-28T10:00:03Z' } },
    { id: 'rethink-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'rethink-user', content: '新的回答', timestamp: '2026-08-28T10:00:04Z' } },
  ] : [
    { id: 'earlier-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '更早的问题', timestamp: '2026-08-28T10:00:00Z' } },
    { id: 'earlier-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'earlier-user', content: '更早的回答', timestamp: '2026-08-28T10:00:01Z' } },
    { id: 'original-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'earlier-answer', content: '需要重新思考的问题', timestamp: '2026-08-28T10:00:02Z' } },
    { id: 'old-answer', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'original-user', content: '不应保留的旧回答', timestamp: '2026-08-28T10:00:03Z' } },
  ];
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/default')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'rethink-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: new Date().toISOString() }) });
    if (path.endsWith('/runtime')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, message: null, updated_at: new Date().toISOString() }) });
    if (path.endsWith('/conversations') && request.method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'rethink-conversation', display_title: '重新思考', lifecycle: 'ACTIVE', streaming_callback_ready: true, model_provider_id: null, model_name: null, reasoning_effort: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() }]) });
    if (path.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: events(), next_cursor: null }) });
    if (path.endsWith('/work-directories')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] }) });
    if (path.endsWith('/workspace')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: { container_id: 'single-runtime' }, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '未配置', note: '' } } }) });
    if (path.endsWith('/context')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ model_name: null, reasoning_effort: null }) });
    if (path.endsWith('/pending-confirmation')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: false }) });
    if (path.endsWith('/original-user/rerun') && request.method() === 'POST') {
      await new Promise<void>(resolve => { releaseRewrite = resolve; });
      rewritten = true;
      return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ accepted: true, cursor: 'rethink-user' }) });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));

  await page.goto('/agent/conversations/rethink-conversation');
  await expect(page.getByText('不应保留的旧回答', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑并重新思考' }).click();
  await page.getByLabel('编辑已发送消息').fill('修改后的问题');
  await page.getByRole('button', { name: '重新思考', exact: true }).click();
  await expect(page.getByText('更早的问题', { exact: true })).toBeVisible();
  await expect(page.getByText('更早的回答', { exact: true })).toBeVisible();
  await expect(page.getByText('不应保留的旧回答', { exact: true })).toHaveCount(0);
  await expect(page.getByText('修改后的问题', { exact: true })).toBeVisible();
  await expect(page.getByText(/已耗时 \d+秒/)).toBeVisible();
  await expect(page.locator('.conversation-turn-status')).toHaveText(/正在思考/);
  await expect.poll(() => Boolean(releaseRewrite)).toBe(true);
  if (!releaseRewrite) throw new Error('Expected rerun request');
  releaseRewrite();
  await expect(page.getByText('新的回答', { exact: true })).toBeVisible();
  await expect(page.getByText('不应保留的旧回答', { exact: true })).toHaveCount(0);
});

test('selected conversation text is sent and rendered as a compact reference card', async ({ page }) => {
  const now = new Date().toISOString();
  const selectedText = '这段内容只能作为会话引用卡片显示';
  let sentPayload: Record<string, unknown> | undefined;
  const events = () => [
    { id: 'reference-source-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '请给出可引用的建议', timestamp: now } },
    { id: 'reference-source-assistant', event_type: 'MESSAGE', payload: { source: 'agent', parent_id: 'reference-source-user', content: selectedText, timestamp: now } },
    ...(sentPayload ? [{ id: 'reference-target-user', event_type: 'MESSAGE', payload: { source: 'user', parent_id: 'reference-source-assistant', content: '请据此继续', conversation_references: sentPayload.references, timestamp: now } }] : []),
  ];
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/default')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'reference-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now }) });
    if (path.endsWith('/runtime')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, updated_at: now }) });
    if (path.endsWith('/conversations') && request.method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'reference-conversation', display_title: '引用会话', lifecycle: 'ACTIVE', streaming_callback_ready: true, model_provider_id: null, model_name: null, reasoning_effort: null, created_at: now, updated_at: now }]) });
    if (path.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: events(), next_cursor: null }) });
    if (path.endsWith('/input-readiness')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ready: true, execution_status: 'idle' }) });
    if (path.endsWith('/work-directories')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] }) });
    if (path.endsWith('/workspace')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: { container_id: 'single-runtime' }, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '未配置', note: '' } } }) });
    if (path.endsWith('/context')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ model_name: null, reasoning_effort: null }) });
    if (path.endsWith('/pending-confirmation')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: false }) });
    if (path.endsWith('/messages') && request.method() === 'POST') {
      sentPayload = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ accepted: true, cursor: 'reference-target-user' }) });
    }
    if (path.endsWith('/capabilities')) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));

  await page.goto('/agent/conversations/reference-conversation');
  const source = page.locator('[data-conversation-event-id="reference-source-assistant"]');
  await expect(source).toContainText(selectedText);
  await source.evaluate(element => {
    const content = element.querySelector('p');
    if (!content) throw new Error('Expected assistant message content');
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(content);
    selection?.removeAllRanges();
    selection?.addRange(range);
    content.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  });
  await page.getByRole('button', { name: '添加到会话' }).click();
  await expect(page.getByLabel('已添加的会话引用')).toContainText('会话引用 1');
  await page.getByLabel('发送 Agent 消息').fill('请据此继续');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect.poll(() => sentPayload).toMatchObject({
    content: '请据此继续',
    references: [{ event_id: 'reference-source-assistant', content: selectedText }],
  });
  const sentMessage = page.locator('[data-user-event-id="reference-target-user"]');
  await expect(sentMessage).toContainText('请据此继续');
  await expect(sentMessage).toContainText('会话引用 1');
  await expect(sentMessage).not.toContainText(selectedText);
  await sentMessage.getByRole('button', { name: '查看会话引用 1' }).click();
  const preview = page.getByRole('dialog', { name: '会话引用内容' });
  await expect(preview).toContainText('所选文本');
  await expect(preview).toContainText(selectedText);
  await preview.getByRole('button', { name: '定位原消息' }).click();
  await expect(preview).toHaveCount(0);
  await expect(source).toBeInViewport();
});

test('Agent workspace groups toggle their conversation lists', async ({ page }) => {
  const now = new Date().toISOString();
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/default')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'collapsible-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: now }) });
    if (path.endsWith('/runtime')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, updated_at: now }) });
    if (path.endsWith('/conversations') && request.method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([
      { id: 'root-conversation', display_title: '根会话', lifecycle: 'ACTIVE', streaming_callback_ready: true, created_at: now, updated_at: now },
      { id: 'project-conversation', display_title: '项目会话', lifecycle: 'ACTIVE', streaming_callback_ready: true, work_directory_id: 'project-directory', created_at: now, updated_at: now },
    ]) });
    if (path.endsWith('/work-directories')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [{ id: 'project-directory', display_name: 'ai-playbook', state: 'ACTIVE', current_version: { id: 'project-directory-v1', version: 1, selected_paths: ['ai-playbook'], working_directory: '/runtime/workspace/project/ai-playbook' } }] }) });
    if (path.endsWith('/workspace')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: { container_id: 'single-runtime' }, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '未配置', note: '' } } }) });
    if (path.endsWith('/capabilities')) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));

  await page.goto('/agent');
  const rootGroup = page.locator('.agent-workspace-group').filter({ hasText: '根工作区' });
  const projectGroup = page.locator('.agent-workspace-group').filter({ hasText: 'ai-playbook' });
  const rootToggle = rootGroup.locator('.agent-workspace-group-toggle');
  const projectToggle = projectGroup.locator('.agent-workspace-group-toggle');

  const rootConversation = rootGroup.getByRole('button', { name: '根会话' });
  await expect(rootConversation).toBeVisible();
  await expect(rootConversation).toHaveCSS('height', '38px');
  await rootToggle.click();
  await expect(rootToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(rootGroup.getByRole('button', { name: '根会话' })).toBeHidden();
  await expect(rootGroup.getByRole('button', { name: '在根工作区中新建会话' })).toBeVisible();
  await rootToggle.click();
  await expect(rootGroup.getByRole('button', { name: '根会话' })).toBeVisible();

  await expect(projectGroup.getByRole('button', { name: '项目会话' })).toBeVisible();
  await projectToggle.click();
  await expect(projectToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(projectGroup.getByRole('button', { name: '项目会话' })).toBeHidden();
  await expect(projectGroup.getByRole('button', { name: '在ai-playbook中新建会话' })).toBeVisible();
  await projectToggle.click();
  await expect(projectGroup.getByRole('button', { name: '项目会话' })).toBeVisible();
});

test('Agent new session keeps full capabilities and can create an explicit workspace', async ({ page }) => {
  const directories: Array<Record<string, unknown>> = [];
  const conversations: Array<Record<string, unknown>> = [];
  const terminalInstanceIds: string[] = [];
  const terminalAttachCounts = new Map<string, number>();
  const destroyedTerminalIds: string[] = [];
  const workspaceScopeRequests: Array<{ bindingId: string | null; workDirectoryId: string | null }> = [];
  let bootstrapPayload: Record<string, unknown> | null = null;
  let attachmentUploads = 0;
  const defaultCapabilities = [
    { id: 'cap-skill', capability_type: 'SKILL', capability_key: 'lark-sheets', digest: 'skill-digest' },
    { id: 'cap-plugin', capability_type: 'PLUGIN', capability_key: 'lark-tools', digest: 'plugin-digest' },
    { id: 'cap-mcp', capability_type: 'MCP', capability_key: 'lark-docs', digest: 'mcp-digest' },
  ];

  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.routeWebSocket('**/agent-workspaces/**/terminal*', socket => {
    const terminalId = new URL(socket.url()).searchParams.get('terminal_instance_id');
    if (terminalId) {
      terminalInstanceIds.push(terminalId);
      terminalAttachCounts.set(terminalId, (terminalAttachCounts.get(terminalId) ?? 0) + 1);
      socket.send(`screen-${terminalId.slice(0, 8)}\r\n$ `);
    }
  });
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/default')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        id: 'fr58-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: new Date().toISOString(),
      }) });
      return;
    }
    if (path.endsWith('/capabilities') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(defaultCapabilities) });
      return;
    }
    if (path.endsWith('/runtime')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        state: 'ACTIVE', write_available: true, message: null, updated_at: new Date().toISOString(),
      }) });
      return;
    }
    if (path.endsWith('/work-directories') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' },
        items: directories,
      }) });
      return;
    }
    if (path.endsWith('/work-directories') && request.method() === 'POST') {
      const payload = JSON.parse(request.postData() ?? '{}') as { display_name: string; selected_paths: string[] };
      const directory = {
        id: 'fr58-frontend', display_name: payload.display_name, state: 'ACTIVE',
        current_version: {
          id: 'fr58-frontend-v1', version: 1, selected_paths: payload.selected_paths,
          working_directory: '/runtime/workspace/project/frontend',
        },
      };
      directories.unshift(directory);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(directory) });
      return;
    }
    if (path.endsWith('/workspace')) {
      workspaceScopeRequests.push({
        bindingId: url.searchParams.get('binding_id'),
        workDirectoryId: url.searchParams.get('work_directory_id'),
      });
      if (url.searchParams.has('binding_id') && url.searchParams.has('work_directory_id')) {
        await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ error: { code: 'AGENT_WORKSPACE_SCOPE_CONFLICT', message: '不能同时指定会话与工作目录' } }) });
        return;
      }
      const scoped = url.searchParams.has('work_directory_id') || url.searchParams.has('binding_id');
      const workingDirectory = scoped ? '/runtime/workspace/project/frontend' : '/runtime/workspace/project';
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: '/runtime/workspace/project',
        scope: scoped ? { kind: 'WORK_DIRECTORY', id: 'fr58-frontend', display_name: '前端工作区' } : { kind: 'ROOT', display_name: '根工作区' },
        working_directory: workingDirectory,
        work_directory: scoped ? directories[0] ?? null : null,
        files: scoped ? [
          { path: '/runtime/workspace/project/frontend/src', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/frontend/src/app.ts', kind: 'file', size: 36 },
        ] : [
          { path: '/runtime/workspace/project/backend', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/frontend', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/frontend/src', kind: 'directory', size: 0 },
          { path: '/runtime/workspace/project/frontend/src/app.ts', kind: 'file', size: 36 },
        ],
        repositories: [{ path: workingDirectory, branch: 'main', head: 'abcdef123456', remote: 'ssh://git.example.test/product.git' }],
        runtime: { container_id: '2fae71c74c89' },
        ide: { workspace_path: workingDirectory, gateway: { supported: true, status: '可通过 SSH 连接', note: '在 JetBrains Gateway 中选择 SSH，并打开以下宿主机目录。', transport: 'SSH_REMOTE', host: 'dev.flowweave.test', port: 2222, user: 'flowweave', path: `/srv/flowweave/workspaces/.agent-workspaces/platform-default/workspace/project${scoped ? '/frontend' : ''}`, ssh_command: 'ssh -p 2222 flowweave@dev.flowweave.test' } },
      }) });
      return;
    }
    if (path.endsWith('/workspace/file')) {
      await route.fulfill({ status: 200, contentType: 'text/plain', body: 'export const app = true;\n' });
      return;
    }
    if (path.endsWith('/attachments') && request.method() === 'POST') {
      attachmentUploads += 1;
      const pasted = attachmentUploads === 2;
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({
        filename: pasted ? '需求说明.md' : '需求.png', mime_type: pasted ? 'text/markdown' : 'image/png', byte_size: 4,
        path: `/runtime/workspace/project/uploads/0123456789abcdef0123456789abcdef-${pasted ? '需求说明.md' : '需求.png'}`,
        image_data_url: pasted ? null : 'data:image/png;base64,iVBORw==',
      }) });
      return;
    }
    if (path.endsWith('/conversations') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(conversations) });
      return;
    }
    if (path.endsWith('/conversations') && request.method() === 'POST') {
      bootstrapPayload = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      const conversation = {
        id: 'fr58-conversation', display_title: '实现前端工作区', title_state: 'PENDING', lifecycle: 'ACTIVE',
        model_provider_id: bootstrapPayload.model_provider_id, model_name: bootstrapPayload.model_name,
        reasoning_effort: bootstrapPayload.reasoning_effort, work_directory_id: 'fr58-frontend',
        work_directory_version_id: 'fr58-frontend-v1', working_directory: '/runtime/workspace/project/frontend',
        streaming_callback_ready: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      };
      conversations.unshift(conversation);
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ conversation, accepted: true, cursor: 'fr58-user' }) });
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [], next_cursor: null }) });
      return;
    }
    if (path.endsWith('/pending-confirmation')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: false }) });
      return;
    }
    if (path.endsWith('/context')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ model_name: 'gpt-pro', reasoning_effort: 'low' }) });
      return;
    }
    const terminalMatch = path.match(/\/terminals\/([^/]+)$/);
    if (terminalMatch && request.method() === 'DELETE') {
      destroyedTerminalIds.push(decodeURIComponent(terminalMatch[1]));
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/capabilities', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { id: 'cap-skill', capability_type: 'SKILL', capability_key: 'lark-sheets', description: '创建和操作飞书电子表格', filename: 'lark-sheets.zip', is_latest: true, document: {} },
      { id: 'cap-plugin', capability_type: 'PLUGIN', capability_key: 'lark-tools', description: '飞书工具集', filename: 'lark-tools.zip', is_latest: true, document: { contributions: { commands: ['summarize'], skills: ['lark-notes'] } } },
      { id: 'cap-mcp', capability_type: 'MCP', capability_key: 'lark-docs', description: '查询飞书文档', filename: 'lark-docs.json', is_latest: true, document: {} },
    ]),
  }));

  await page.route('**/api/v1/model-providers', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{
      id: 'provider-default', name: '默认供应商', connection_state: 'CONNECTED',
      models: [{ model_name: 'gpt-default', enabled: true, is_default: true }],
    }, {
      id: 'provider-pro', name: '专业供应商', connection_state: 'CONNECTED',
      models: [{ model_name: 'gpt-pro', enabled: true, is_default: true, default_reasoning_effort: 'high', supported_reasoning_efforts: ['low', 'high'] }],
    }]),
  }));

  await login(page);
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  await page.getByRole('button', { name: '新增工作区', exact: true }).click();
  const creator = page.getByRole('dialog', { name: '新增工作区' });
  await expect(creator).toBeVisible();
  await expect(creator.getByRole('tree', { name: '项目目录树' })).toBeVisible();
  await expect(creator.getByRole('checkbox', { name: 'frontend/src', exact: true })).toBeVisible();
  await creator.getByRole('button', { name: '收起目录 frontend', exact: true }).click();
  await expect(creator.getByRole('checkbox', { name: 'frontend/src', exact: true })).toBeHidden();
  await creator.getByRole('button', { name: '展开目录 frontend', exact: true }).click();
  await creator.getByRole('checkbox', { name: 'frontend/src', exact: true }).check();
  await expect(creator.getByRole('checkbox', { name: 'frontend', exact: true })).not.toBeChecked();
  await creator.getByRole('checkbox', { name: 'frontend', exact: true }).check();
  await expect(creator.getByRole('checkbox', { name: 'frontend/src', exact: true })).not.toBeChecked();
  await creator.getByLabel('工作区名称').fill('前端工作区');
  await creator.getByRole('button', { name: '创建工作区' }).click();
  await expect(page.locator('.agent-workspace-group').getByText('前端工作区', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '在前端工作区中新建会话' }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(page.getByText('会话已就绪', { exact: true })).toBeVisible();
  await expect(page.getByText('草稿', { exact: true })).toHaveCount(0);
  expect(conversations).toHaveLength(0);
  expect(bootstrapPayload).toBeNull();

  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('$');
  const skillMenu = page.getByRole('listbox', { name: '选择技能' });
  await expect(skillMenu.getByRole('option', { name: /lark-sheets/ })).toBeVisible();
  await expect(skillMenu.getByRole('option', { name: /lark-notes/ })).toBeVisible();
  await composer.press('Enter');
  await expect(composer).toHaveValue('$lark-sheets ');

  await composer.fill('/');
  const commandMenu = page.getByRole('listbox', { name: '选择命令或 MCP' });
  await expect(commandMenu.getByRole('option', { name: /summarize/ })).toBeVisible();
  await expect(commandMenu.getByRole('option', { name: /lark-docs/ })).toBeVisible();
  await composer.press('Enter');
  await expect(composer).toHaveValue('/lark-tools:summarize ');
  await composer.fill('');

  await page.getByLabel('打开模型与推理设置').click();
  await page.getByLabel('会话供应商', { exact: true }).selectOption('provider-pro');
  await page.getByLabel('思考程度').selectOption('low');
  await page.getByLabel('上传附件').setInputFiles({ name: '需求.png', mimeType: 'image/png', buffer: Buffer.from([1, 2, 3, 4]) });
  await expect(page.locator('.agent-attachments').getByText('需求.png', { exact: true })).toBeVisible();
  expect(attachmentUploads).toBe(1);
  await composer.evaluate(element => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(['# 需求说明'], '需求说明.md', { type: 'text/markdown' }));
    element.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  await expect(page.locator('.agent-attachments').getByText('需求说明.md', { exact: true })).toBeVisible();
  expect(attachmentUploads).toBe(2);

  await composer.fill('切换页面后仍应保留的首条草稿');
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新会话' })).toBeVisible();
  await expect(page.getByLabel('发送 Agent 消息')).toHaveValue('切换页面后仍应保留的首条草稿');
  await expect(page.locator('.agent-attachments').getByText('需求.png', { exact: true })).toBeVisible();
  await expect(page.locator('.agent-attachments').getByText('需求说明.md', { exact: true })).toBeVisible();
  await expect(page.getByText('当前供应商：专业供应商 · 前端工作区', { exact: true })).toBeVisible();
  expect(conversations).toHaveLength(0);
  expect(bootstrapPayload).toBeNull();

  await expect(page.getByText('环境信息', { exact: true })).toBeVisible();
  const environmentSummary = page.locator('.agent-workspace-overview');
  await expect(environmentSummary.getByText('前端工作区', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByText('main', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByText('abcdef123456', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByText('ssh://git.example.test/product.git', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByText('ssh -p 2222 flowweave@dev.flowweave.test', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByText('/srv/flowweave/workspaces/.agent-workspaces/platform-default/workspace/project/frontend', { exact: true })).toBeVisible();
  await expect(environmentSummary.getByRole('button', { name: '复制 SSH 与目录', exact: true })).toBeVisible();
  expect(workspaceScopeRequests.at(-1)).toEqual({ bindingId: null, workDirectoryId: 'fr58-frontend' });
  await expect(page.getByText('2fae71c74c89', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新终端', exact: true }).click();
  await page.getByLabel('新增工作区工具').click();
  await page.getByRole('button', { name: '终端', exact: true }).click();
  await expect.poll(() => new Set(terminalInstanceIds).size).toBe(2);
  await expect(page.getByRole('button', { name: '2fae71c74c89', exact: true })).toHaveCount(2);
  const initialTerminalIds = [...new Set(terminalInstanceIds)];
  expect(initialTerminalIds[0]).not.toBe(initialTerminalIds[1]);
  await expect(page.locator('.agent-terminal-tab-panel.active')).toContainText(`screen-${initialTerminalIds[1].slice(0, 8)}`);
  await page.getByRole('button', { name: '2fae71c74c89', exact: true }).first().click();
  await expect(page.locator('.agent-terminal-tab-panel.active')).toContainText(`screen-${initialTerminalIds[0].slice(0, 8)}`);
  await expect(page.locator('.agent-terminal-tab-panel.active')).not.toContainText(`screen-${initialTerminalIds[1].slice(0, 8)}`);

  await page.getByLabel('新增工作区工具').click();
  await page.getByRole('button', { name: '文件', exact: true }).click();
  await page.getByLabel('新增工作区工具').click();
  await page.locator('.agent-workspace-tool-actions').getByRole('button', { name: '文件', exact: true }).click();
  await expect(page.locator('.agent-workspace-tabs .agent-workspace-tab-select').filter({ hasText: '文件' })).toHaveCount(1);
  await expect(page.getByText('app.ts', { exact: true })).toBeVisible();
  expect(new Set(terminalInstanceIds)).toEqual(new Set(initialTerminalIds));

  await page.getByLabel('发送 Agent 消息').fill('实现前端工作区');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page).toHaveURL(/\/agent\/conversations\/fr58-conversation$/);
  expect(bootstrapPayload).toMatchObject({
    model_provider_id: 'provider-pro', model_name: 'gpt-pro', reasoning_effort: 'low',
    work_directory_id: 'fr58-frontend', content: '实现前端工作区',
    attachments: [
      { path: '/runtime/workspace/project/uploads/0123456789abcdef0123456789abcdef-需求.png', image_data_url: 'data:image/png;base64,iVBORw==' },
      { path: '/runtime/workspace/project/uploads/0123456789abcdef0123456789abcdef-需求说明.md', image_data_url: null },
    ],
  });
  await expect(page.locator('.agent-workspace-overview').getByText('前端工作区', { exact: true })).toBeHidden();
  await expect.poll(() => workspaceScopeRequests.at(-1)).toEqual({ bindingId: 'fr58-conversation', workDirectoryId: null });
  await expect(page.getByRole('button', { name: '2fae71c74c89', exact: true })).toHaveCount(2);
  expect(new Set(terminalInstanceIds)).toEqual(new Set(initialTerminalIds));

  await page.locator('.agent-workspace-tool-actions').getByLabel('关闭工作区工具').click();
  await expect(page.getByText('环境信息', { exact: true })).toBeVisible();
  expect(destroyedTerminalIds).toHaveLength(0);
  await page.getByRole('button', { name: '节点资产', exact: true }).click();
  await page.getByRole('button', { name: 'Agent 会话' }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/agent/conversations/fr58-conversation');
  await expect(page.getByRole('heading', { name: '实现前端工作区' })).toBeVisible();
  await page.locator('.agent-workspace-summary').getByLabel('打开工作区工具').click();
  const restoredTerminalTabs = page.getByRole('button', { name: '2fae71c74c89', exact: true });
  await expect(restoredTerminalTabs).toHaveCount(2);
  await restoredTerminalTabs.first().click();
  await expect.poll(() => terminalAttachCounts.get(initialTerminalIds[0]) ?? 0).toBeGreaterThanOrEqual(2);
  await expect(page.locator('.agent-terminal-tab-panel.active')).toContainText(`screen-${initialTerminalIds[0].slice(0, 8)}`);
  await restoredTerminalTabs.last().click();
  await expect.poll(() => terminalAttachCounts.get(initialTerminalIds[1]) ?? 0).toBeGreaterThanOrEqual(2);
  await expect(page.locator('.agent-terminal-tab-panel.active')).toContainText(`screen-${initialTerminalIds[1].slice(0, 8)}`);
  expect(new Set(terminalInstanceIds)).toEqual(new Set(initialTerminalIds));

  await page.getByRole('button', { name: '关闭终端 2fae71c74c89页签' }).first().click();
  const closeTerminalDialog = page.getByRole('dialog', { name: '关闭此终端？' });
  await expect(closeTerminalDialog).toBeVisible();
  await expect(closeTerminalDialog).toContainText('正在执行的命令');
  await closeTerminalDialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(closeTerminalDialog).toBeHidden();
  await expect.poll(() => destroyedTerminalIds.length).toBe(0);
  await expect(page.getByRole('button', { name: '2fae71c74c89', exact: true })).toHaveCount(2);

  await page.getByRole('button', { name: '关闭终端 2fae71c74c89页签' }).first().click();
  await expect(closeTerminalDialog).toBeVisible();
  await closeTerminalDialog.getByRole('button', { name: '关闭终端', exact: true }).click();
  await expect.poll(() => destroyedTerminalIds.length).toBe(1);
  expect([...new Set(terminalInstanceIds)]).toContain(destroyedTerminalIds[0]);
  await expect(page.getByRole('button', { name: '2fae71c74c89', exact: true })).toHaveCount(1);
  await page.getByLabel('新增工作区工具').click();
  await page.getByRole('button', { name: '终端', exact: true }).click();
  await expect.poll(() => new Set(terminalInstanceIds).size).toBe(3);
  const reopenedId = [...new Set(terminalInstanceIds)].find(id => !initialTerminalIds.includes(id));
  expect(reopenedId).toBeTruthy();
  expect(reopenedId).not.toBe(destroyedTerminalIds[0]);
});

test('Agent workspace drawer remains available while Runtime is recovering', async ({ page }) => {
  const conversation = {
    id: 'recovering-conversation', workspace_id: 'recovering-workspace',
    external_conversation_id: 'external-recovering-conversation',
    display_title: '保留的历史会话', title_state: 'READY', lifecycle: 'ACTIVE',
    working_directory: '/runtime/workspace/project', work_directory_id: null,
    model_provider_id: null, model_name: null, reasoning_effort: null,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  };
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/default')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        id: 'recovering-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: new Date().toISOString(),
      }) });
      return;
    }
    if (path.endsWith('/runtime')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        state: 'RECOVERING', write_available: false, message: '运行环境正在恢复，数据已保留', updated_at: new Date().toISOString(),
      }) });
      return;
    }
    if (path.endsWith('/conversations')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([conversation]) });
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        events: [{ id: 'recovering-user-event', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '这条历史消息必须保留可见', timestamp: new Date().toISOString() } }],
        head_id: 'recovering-user-event',
      }) });
      return;
    }
    if (path.endsWith('/context')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ model_name: null, reasoning_effort: null }) });
      return;
    }
    if (path.endsWith('/pending-confirmation')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: false }) });
      return;
    }
    if (path.endsWith('/work-directories')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [],
      }) });
      return;
    }
    if (path.endsWith('/workspace')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project',
        work_directory: null, files: [{ path: '/runtime/workspace/project/README.md', kind: 'file', size: 12 }],
        repositories: [], runtime: { container_id: null },
        ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: true, status: '可通过 SSH 连接', note: '在 JetBrains Gateway 中选择 SSH，并打开以下宿主机目录。', transport: 'SSH_REMOTE', host: 'dev.flowweave.test', port: 2222, user: 'flowweave', path: '/srv/flowweave/workspaces/.agent-workspaces/platform-default/workspace/project', ssh_command: 'ssh -p 2222 flowweave@dev.flowweave.test' } },
      }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'AGENT_RUNTIME_RECOVERING', message: '运行环境正在恢复' } }) });
  });
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));

  await page.goto('/agent/conversations/recovering-conversation');
  await expect(page.getByText('这条历史消息必须保留可见')).toBeVisible();
  await expect(page.getByText('运行环境正在恢复', { exact: true })).toBeVisible();
  const workspaceDrawer = page.getByRole('complementary').filter({ hasText: 'WORKSPACE' });
  await expect(
    workspaceDrawer.getByRole('article').filter({ hasText: '当前工作区' }).getByRole('code'),
  ).toHaveText('/runtime/workspace/project');
  await expect(workspaceDrawer.getByRole('button', { name: 'SSH 接入说明', exact: true })).toBeVisible();
  await expect(workspaceDrawer.getByText('用户名', { exact: true })).not.toBeVisible();
  await workspaceDrawer.getByRole('button', { name: 'SSH 接入说明', exact: true }).click();
  const sshGuide = page.getByRole('dialog', { name: 'SSH 接入说明' });
  await expect(sshGuide.getByText('主机 / IP', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('dev.flowweave.test', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('端口', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('2222', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('当前会话工作目录', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('/srv/flowweave/workspaces/.agent-workspaces/platform-default/workspace/project', { exact: true })).toBeVisible();
  await expect(sshGuide.getByText('当前尚未具备会话级 SSH 隔离', { exact: true })).toBeVisible();
  await expect(sshGuide.getByRole('button', { name: '复制主机 / IP', exact: true })).toBeVisible();
  await expect(sshGuide.getByRole('button', { name: '复制端口', exact: true })).toBeVisible();
  await expect(sshGuide.getByRole('button', { name: '复制当前会话工作目录', exact: true })).toBeVisible();
  await sshGuide.getByRole('button', { name: '我已了解', exact: true }).click();
  await expect(sshGuide).toBeHidden();
  await expect(workspaceDrawer.getByRole('button', { name: '新终端', exact: true })).toBeDisabled();
  await workspaceDrawer.getByRole('button', { name: '文件', exact: true }).click();
  await expect(page.getByText('README.md', { exact: true })).toBeVisible();
});

test('node asset editor and repeated flow-node canvas match the product model', async ({ page }) => {
  await login(page);

  await page.getByRole('button', { name: '节点资产' }).click();

  const assetName = `UI节点资产-${suffix}`;
  await page.getByRole('button', { name: '新建节点' }).click();
  const editor = page.locator('form.asset-editor');
  await editor.getByLabel('节点名称').fill(assetName);
  await editor.getByLabel('节点说明').fill('四步节点资产编辑器验收');
  await editor.getByRole('button', { name: '下一步' }).click();
  await editor.getByLabel('启动触发提示词').fill('读取输入并执行节点任务');
  await editor.getByRole('button', { name: '下一步' }).click();
  await expect(editor.getByRole('heading', { name: '输入定义' })).toBeVisible();
  await expect(editor.getByRole('heading', { name: '输出定义' })).toBeVisible();
  await expect(editor.locator('.io-empty')).toHaveCount(2);
  await editor.getByRole('button', { name: '添加输入' }).click();
  await editor.getByRole('button', { name: '添加输出' }).click();
  await expect(editor.getByLabel('inputs key 0')).toHaveValue('input_1');
  await expect(editor.getByLabel('outputs key 0')).toHaveValue('output_1');
  await expect(editor.getByLabel('inputs type 0')).toHaveValue('URL');
  await editor.getByLabel('inputs type 0').selectOption('FILE');
  await expect(editor.getByLabel('inputs type 0')).toHaveValue('FILE');
  await editor.getByLabel('inputs type 0').selectOption('URL');
  await expect(editor.getByLabel('outputs type 0')).toHaveValue('URL');
  await editor.getByLabel('inputs name 0').fill('输入产物');
  await editor.getByLabel('outputs name 0').fill('输出产物');
  const card = page.getByTestId('node-card').filter({ hasText: assetName }).last();
  const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/node-assets') && response.request().method() === 'POST');
  await editor.evaluate((form: HTMLFormElement) => form.requestSubmit());
  const savedResponse = await saved;
  expect(savedResponse.ok()).toBeTruthy();
  await expect(card).toBeVisible();
  await card.click();
  const detail = page.getByRole('dialog', { name: `节点详情 ${assetName}` });
  await expect(detail).toContainText('读取输入并执行节点任务');
  await detail.getByRole('button', { name: '关闭节点详情' }).click();

  await page.getByRole('button', { name: '流程编排' }).click();
  await page.getByRole('button', { name: '新建流程' }).click();
  const library = page.getByTestId('flow-library');
  const assetButton = library.getByRole('button', { name: assetName, exact: true }).last();
  const canvas = page.getByTestId('flow-designer');
  await expect(canvas.locator('.react-flow__pane')).toBeVisible();
  await expect(canvas.getByRole('button', { name: '流程走向' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('飞书 Wiki 根节点')).toHaveCount(0);
  await dropAsset(page, assetButton, canvas, { x: 320, y: 260 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await dropAsset(page, assetButton, canvas, { x: 700, y: 360 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await expect(canvas.getByRole('status')).toContainText('再次添加');
  await canvas.getByRole('button', { name: '流程走向' }).click();
  await connectFlow(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await canvas.locator('.react-flow__node').nth(1).getByRole('button', { name: `删除节点 ${assetName}` }).click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(canvas.getByRole('status')).toContainText('关联连线');
  await dropAsset(page, assetButton, canvas, { x: 700, y: 360 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await connectFlow(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1);
  await canvas.getByRole('button', { name: '产物流转' }).click();
  await connectArtifact(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(2);
  await page.getByRole('button', { name: '自动布局' }).click();
  await canvas.locator('.react-flow__node').nth(0).click();
  await page.getByRole('button', { name: '添加开始门禁' }).click();
  await page.getByRole('button', { name: '添加结束门禁' }).click();
  await expect(page.locator('.gate-row')).toHaveCount(2);
  await page.getByRole('button', { name: `删除节点 ${assetName}` }).last().click();
  await expect(canvas.locator('.react-flow__node')).toHaveCount(1);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0);
  await expect(page.getByLabel('默认入口')).toHaveValue('');
  await dropAsset(page, assetButton, canvas, { x: 320, y: 260 });
  await expect(canvas.locator('.react-flow__node')).toHaveCount(2);
  await page.getByLabel('流程名称').fill(`UI流程-${suffix}`);
  await expect(page.getByLabel('运行环境版本')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '保存流程' })).toBeEnabled();
  const flowSaved = page.waitForResponse(response => response.url().endsWith('/api/v1/flows') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '保存流程' }).click();
  const flowResponse = await flowSaved;
  expect(flowResponse.ok(), await flowResponse.text()).toBeTruthy();
  await expect(library.getByRole('button', { name: `UI流程-${suffix}`, exact: true })).toBeVisible();
});

test('flow canvas uses application names instead of variable keys', async ({ page, request }) => {
  const assetName = `端口名称-${suffix}`;
  const asset = await createAsset(request, assetName);
  const flow = await post(request, '/flows', {
    name: `端口名称流程-${suffix}`,
    description: '验证流程画布使用展示名称',
    default_entry_key: null,
    nodes: [
      { instance_key: 'source', node_asset_id: asset.id, alias: null, position_x: 100, position_y: 160, config_override: {}, gates: [] },
      { instance_key: 'target', node_asset_id: asset.id, alias: null, position_x: 500, position_y: 160, config_override: {}, gates: [] },
    ],
    edges: [{ source_instance_key: 'source', target_instance_key: 'target', position: 0 }],
    port_mappings: [{ source_instance_key: 'source', source_output_key: 'design', target_instance_key: 'target', target_input_key: 'prd' }],
  });
  await login(page);
  await page.getByRole('button', { name: '流程编排' }).click();
  await page.getByTestId('flow-library').getByRole('button', { name: flow.name, exact: true }).click();

  const canvas = page.getByTestId('flow-designer');
  await expect(canvas.getByText('需求文档', { exact: true })).toHaveCount(2);
  await expect(canvas.getByText('技术方案', { exact: true })).toHaveCount(2);
  await expect(canvas.getByText('prd', { exact: true })).toHaveCount(0);
  await expect(canvas.getByText('design', { exact: true })).toHaveCount(0);
  await expect(canvas.getByText('技术方案 → 需求文档', { exact: true })).toBeVisible();
  await canvas.getByRole('button', { name: '产物流转' }).click();
  await connectArtifact(canvas.locator('.react-flow__node').nth(0), canvas.locator('.react-flow__node').nth(1));
  await expect(canvas.getByRole('status')).toContainText(`${assetName}.技术方案 → ${assetName}.需求文档`);
});

test('run keeps attempts, snapshots, gates and artifact lineage visible', async ({ page, request }) => {
  const asset = await createAsset(request, `运行资产-${suffix}`);
  const flow = await createFlow(request, asset.id, `运行流程-${suffix}`);
  await login(page);
  await page.getByRole('button', { name: '流程运行' }).click();
  const runGroup = page.locator('.run-group').filter({ hasText: flow.name });
  await expect(runGroup).toBeVisible();
  await runGroup.getByRole('button', { name: '启动', exact: true }).click();
  const dialog = page.locator('form.start-run-modal');
  await dialog.getByLabel('运行名称').fill(`运行验收-${suffix}`);
  await dialog.getByLabel('本次运行环境版本').selectOption(await readyEnvironmentVersionId(request));
  const createdRunResponse = page.waitForResponse(response => response.url().endsWith(`/api/v1/flows/${flow.id}/runs`) && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '启动流程', exact: true }).click();
  const createdRun = await (await createdRunResponse).json();
  await expect(page.getByText('点击任意节点，在右侧配置输入并开始一次独立执行', { exact: true })).toBeVisible();
  await expect(page.getByText('FlowRun 会话工作台', { exact: true })).toHaveCount(0);
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/api/v1/flow-runs/${createdRun.id}/conversations`);
    return (await response.json()).length;
  }).toBe(0);
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/api/v1/flow-runs/${createdRun.id}/runtime`);
    return (await response.json()).connection_state;
  }, { timeout: 60_000 }).toBe('READY');
  await expect(page.locator('.action-panel')).toHaveCount(0);
  const graphNodes = page.locator('.run-graph .react-flow__node');
  await graphNodes.filter({ hasText: '首轮方案' }).click();
  const nodeConsole = page.locator('.node-console');
  await expect(nodeConsole).toContainText('首轮方案');
  await expect(nodeConsole).toContainText('创建一次独立执行');
  await expect(nodeConsole).toContainText('使用 Skill 启动');
  await expect(nodeConsole).toContainText('发送启动提示词');
  await expect(nodeConsole).toContainText('仅创建会话启动');

  await expect(nodeConsole.getByRole('heading', { name: '本次输入' })).toBeVisible();
  await expect(nodeConsole.getByText('prd · URL', { exact: true })).toBeVisible();
  await nodeConsole.getByLabel('新建产物名称 prd').fill(`需求文档-${suffix}`);
  await nodeConsole.getByLabel('新建产物 URL prd').fill(`https://files.example.test/e2e-input-${suffix}`);
  await nodeConsole.getByRole('button', { name: '使用这个输入' }).click();
  await expect(nodeConsole.locator('.selected-artifact')).toContainText(`需求文档-${suffix}`);
  await expect(nodeConsole.locator('.selected-artifact')).toContainText(`https://files.example.test/e2e-input-${suffix}`);
  await nodeConsole.getByText('发送启动提示词', { exact: true }).click();
  await nodeConsole.getByRole('button', { name: '开始第 1 次执行' }).click();
  const attemptControl = page.locator('.attempt-control');
  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await attemptControl.getByRole('button', { name: '进入节点会话' }).click();
  const nodeSessionUrl = new RegExp(`/flow-runs/${createdRun.id}/nodes/[^/]+/attempts/[^/]+/agent-sessions$`);
  await expect(page).toHaveURL(nodeSessionUrl);
  await expect(page.getByRole('heading', { name: '首轮方案', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '新会话', exact: true })).toHaveCount(0);
  await expect(page.getByRole('navigation').getByRole('button', { name: '流程运行' })).toHaveClass(/active/);
  await expect(page.getByRole('navigation').getByRole('button', { name: 'Agent 会话' })).not.toHaveClass(/active/);
  await expect(page.getByRole('button', { name: '返回节点执行' })).toBeVisible();
  await expect(page.getByText('FlowRun 会话工作台', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '新增工作区' })).toBeVisible();
  await expect(page.locator('.agent-workbench-rail-footer')).toHaveCount(0);
  await expect(page.getByLabel('添加附件')).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(nodeSessionUrl);
  await expect(page.getByRole('heading', { name: '首轮方案', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '返回节点执行' }).click();
  await expect(page.locator('.attempt-control')).toBeVisible();

  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await expect(attemptControl.getByRole('heading', { name: '启动这条执行记录' })).toHaveCount(0);
  await expect(attemptControl.getByRole('button', { name: '确认启动' })).toHaveCount(0);
  await graphNodes.filter({ hasText: '首轮方案' }).click();
  await expect(page.locator('.run-graph .run-graph-node.snapshot-selected')).toHaveCount(1);
  await page.locator('.run-rail .timeline').getByRole('button', { name: /首轮方案/ }).click();
  await expect(page.locator('.run-graph .run-graph-node.snapshot-selected')).toHaveCount(0);
  await expect(attemptControl).toContainText('首轮方案');
  const nodeSessionEntry = attemptControl.getByRole('button', { name: '进入节点会话' });
  await expect(nodeSessionEntry).toBeVisible();
  await expect(attemptControl.locator('.attempt-runtime-summary')).toHaveCount(0);
  expect(await attemptControl.evaluate(panel => {
    const state = panel.querySelector('.state-banner');
    const session = panel.querySelector('.node-session-entry');
    const frozenInputs = Array.from(panel.querySelectorAll('.attempt-side-section')).find(section => section.textContent?.includes('本轮冻结输入'));
    return Boolean(state && session && frozenInputs
      && (state.compareDocumentPosition(session) & Node.DOCUMENT_POSITION_FOLLOWING)
      && (session.compareDocumentPosition(frozenInputs) & Node.DOCUMENT_POSITION_FOLLOWING));
  })).toBeTruthy();
  await expect(attemptControl.locator('.attempt-input-card')).toContainText(`需求文档-${suffix}`);
  await expect(attemptControl.locator('.attempt-input-card')).toContainText(`https://files.example.test/e2e-input-${suffix}`);
  await expect(attemptControl.getByRole('region', { name: '节点输出' })).toContainText('本轮尚无输出');
  await expect(attemptControl.locator('.gate-results')).toContainText('START');

  const changedFlow = await request.put(`${apiBase}/api/v1/flows/${flow.id}`, {
    data: {
      name: flow.name,
      description: '运行中发布的新流程配置',
      default_entry_key: flow.default_entry_key,
      row_version: flow.row_version,
      nodes: flow.nodes.map((node: { instance_key: string; node_asset_id: string; alias?: string | null; position_x: number; position_y: number; config_override: Record<string, unknown>; gates: Array<{ stage: string; position: number; gate_type: string; enabled: boolean; timeout_seconds: number; config: Record<string, unknown> }> }) => ({
        instance_key: node.instance_key,
        node_asset_id: node.node_asset_id,
        alias: node.alias,
        position_x: node.position_x,
        position_y: node.position_y,
        config_override: node.config_override,
        gates: node.gates.map(gate => ({
          stage: gate.stage, position: gate.position, gate_type: gate.gate_type,
          enabled: gate.enabled, timeout_seconds: gate.timeout_seconds, config: gate.config,
        })),
      })),
      edges: flow.edges.map((edge: { source_instance_key: string; target_instance_key: string; position: number }) => ({
        source_instance_key: edge.source_instance_key,
        target_instance_key: edge.target_instance_key,
        position: edge.position,
      })),
      port_mappings: flow.port_mappings.map((mapping: {
        source_instance_key: string; source_output_key: string;
        target_instance_key: string; target_input_key: string;
      }) => ({
        source_instance_key: mapping.source_instance_key,
        source_output_key: mapping.source_output_key,
        target_instance_key: mapping.target_instance_key,
        target_input_key: mapping.target_input_key,
      })),
    },
  });
  expect(changedFlow.ok(), await changedFlow.text()).toBeTruthy();
  await expect(page.getByTestId('snapshot-sync')).toBeVisible();
  await page.getByRole('button', { name: '同步最新配置' }).click();
  await expect(page.locator('.run-title')).toContainText('流程快照 v2');
  await expect(page.getByTestId('snapshot-sync')).toBeHidden();

  await attemptControl.getByRole('button', { name: '创建新的独立执行' }).click();
  await expect(nodeConsole).toContainText('已有执行记录');
  await nodeConsole.getByLabel('节点输入 prd').selectOption({ index: 1 });
  await nodeConsole.getByText('发送启动提示词', { exact: true }).click();
  await nodeConsole.getByRole('button', { name: '开始第 2 次执行' }).click();
  await expect(page.getByTestId('attempt-state')).toHaveText('EXECUTING');
  await expect(page.locator('.timeline button')).toHaveCount(2);
  await expect(attemptControl.getByRole('button', { name: '取消整个流程' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '取消整个流程' })).toHaveCount(0);
  await confirmProductDialog(
    page,
    attemptControl.getByRole('button', { name: '取消本轮节点执行' }),
    '取消本轮执行',
    '其他节点执行和整个流程不会被取消',
  );
  await expect(page.locator('.run-side-panel')).toHaveCount(0);
  await expect(page.locator('.timeline button.active')).toHaveCount(0);
  await expect(page.getByTestId('flow-run-state')).toHaveText('运行中');

  const runResponse = await request.get(`${apiBase}/api/v1/flow-runs/${createdRun.id}`);
  expect(runResponse.ok(), await runResponse.text()).toBeTruthy();
  const run = await runResponse.json();
  expect(run.environment_version?.image_digest).toMatch(/^sha256:/);
  expect(run.artifacts).toEqual(expect.arrayContaining([expect.objectContaining({
    field_key: 'prd',
    uri: `https://files.example.test/e2e-input-${suffix}`,
    source: 'HUMAN',
    metadata: expect.objectContaining({
      source: 'HUMAN_INPUT',
      display_name: `需求文档-${suffix}`,
    }),
  })]));
  expect(run.node_runs[0].attempts[0].input_bindings).toEqual(expect.arrayContaining([
    expect.objectContaining({ input_field_key: 'prd' }),
  ]));
  expect(run.node_runs.filter((item: { flow_node_snapshot_key: string }) => item.flow_node_snapshot_key === 'design_a')).toHaveLength(2);
  expect(run.state).not.toBe('CANCELLED');
  expect(run.node_runs).toEqual(expect.arrayContaining([
    expect.objectContaining({ state: 'ACTIVE' }),
    expect.objectContaining({ state: 'CANCELLED' }),
  ]));
});

test('cancelled run becomes read-only and can be permanently deleted', async ({ page, request }) => {
  const asset = await createAsset(request, `终态资产-${suffix}`);
  const flow = await createFlow(request, asset.id, `终态流程-${suffix}`);
  const started = await post(request, `/flows/${flow.id}/runs`, {
    name: `终态运行-${suffix}`,
    environment_version_id: await readyEnvironmentVersionId(request),
  });
  await post(request, `/flow-runs/${started.id}/nodes/design_a/runs`, { artifact_ids: {} });
  await post(request, `/flow-runs/${started.id}/nodes/design_b/runs`, { artifact_ids: {} });

  await login(page);
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.getByLabel('搜索流程或运行').fill(`终态运行-${suffix}`);
  await page.locator('.run-open').click();
  await expect(page.locator('.timeline button')).toHaveCount(2);
  await page.locator('.timeline button').first().click();

  const cancelled = await request.post(`${apiBase}/api/v1/flow-runs/${started.id}/cancel`, {
    headers: { 'Idempotency-Key': `cancel-terminal-${suffix}` },
  });
  expect(cancelled.ok(), await cancelled.text()).toBeTruthy();
  await page.reload();
  await expect(page.getByTestId('flow-run-state')).toHaveText('已取消');
  await expect(page.locator('.run-progress')).toContainText('0 已验收');
  await expect(page.locator('.run-progress')).toContainText('2 已结束');
  await expect(page.locator('.run-progress')).toContainText('0 进行中');
  await expect(page.locator('.terminal-run-panel')).toContainText('流程已取消');
  await expect(page.getByRole('button', { name: '创建新的独立执行' })).toBeHidden();
  await expect(page.getByRole('button', { name: '取消整个流程' })).toBeHidden();

  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '永久删除此运行' }),
    '永久删除',
  );
  await expect(page.getByLabel('搜索流程或运行')).toBeVisible();
  const listed = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect((await listed.json()).some((item: { id: string }) => item.id === started.id)).toBeFalsy();
});

test('corrupt and legacy browser state recover without a blank page', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto('/');

  await page.evaluate(() => localStorage.setItem('flowweave-workbench', '{broken-json'));
  await page.reload();
  await expect(page.getByRole('heading', { name: '节点资产', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('flowweave-workbench'))).toBeNull();

  await page.evaluate(() => localStorage.setItem('flowweave-workbench', JSON.stringify({
    version: 0,
    state: { view: 'legacy-dashboard', selectedRunId: 42, selectedNodeRunId: false },
  })));
  await page.reload();
  await expect(page.getByRole('heading', { name: '节点资产', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const raw = localStorage.getItem('flowweave-workbench');
    return raw ? JSON.parse(raw).version : undefined;
  })).toBe(4);
  const persisted = await page.evaluate(() => JSON.parse(localStorage.getItem('flowweave-workbench') ?? '{}'));
  expect(persisted.state).toEqual({ view: 'nodes' });

  await page.evaluate(() => localStorage.setItem('flowweave-workbench', JSON.stringify({
    version: 2,
    state: {
      view: 'agent-chat',
      selectedRunId: 'deleted-run',
      selectedNodeRunId: 'deleted-node-run',
      selectedAttemptId: 'deleted-attempt',
      selectedConversationId: 'deleted-conversation',
    },
  })));
  await page.reload();
  await expect(page.getByRole('heading', { name: '流程运行', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const raw = localStorage.getItem('flowweave-workbench');
    return raw ? JSON.parse(raw).state : undefined;
  })).toEqual({ view: 'runs' });

  await page.getByRole('button', { name: '流程编排' }).click();
  await expect(page.getByTestId('flow-designer')).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('node assets, flows and runs support single and filtered bulk deletion', async ({ page, request }) => {
  const marker = `删除管理-${Date.now().toString(36)}`;
  const singleAsset = await createAsset(request, `${marker}-节点-单删`);
  const bulkAssetA = await createAsset(request, `${marker}-节点-批量-A`);
  const bulkAssetB = await createAsset(request, `${marker}-节点-批量-B`);
  const flowAsset = await createAsset(request, `${marker}-流程依赖节点`);
  const singleFlow = await createFlow(request, flowAsset.id, `${marker}-流程-单删`);
  const bulkFlowA = await createFlow(request, flowAsset.id, `${marker}-流程-批量-A`);
  const bulkFlowB = await createFlow(request, flowAsset.id, `${marker}-流程-批量-B`);
  const runs = await Promise.all([
    post(request, `/flows/${singleFlow.id}/runs`, { name: `${marker}-运行-单删`, environment_version_id: await readyEnvironmentVersionId(request) }),
    post(request, `/flows/${bulkFlowA.id}/runs`, { name: `${marker}-运行-批量-A`, environment_version_id: await readyEnvironmentVersionId(request) }),
    post(request, `/flows/${bulkFlowB.id}/runs`, { name: `${marker}-运行-批量-B`, environment_version_id: await readyEnvironmentVersionId(request) }),
  ]);

  await login(page);

  // Node assets: one explicit row action and filtered-result bulk deletion.
  await page.getByLabel('搜索节点').fill(`${marker}-节点`);
  await expect(page.getByTestId('node-card')).toHaveCount(3);
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: `删除节点资产 ${singleAsset.name}` }),
    '确认删除',
  );
  await expect(page.getByTestId('node-card')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '批量删除 (2)' }),
    '确认删除',
  );
  await expect(page.getByTestId('node-card')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已删除 2 个节点资产');

  // Flow definitions with runs are protected until their runs are deleted.
  await page.getByRole('button', { name: '流程编排' }).click();
  const flowLibrary = page.getByTestId('flow-library');
  await flowLibrary.getByLabel('搜索流程').fill(`${marker}-流程`);
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: `删除流程 ${singleFlow.name}` }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await expect(page.locator('.canvas-error')).toContainText('请先删除关联运行');

  // Delete runs before permanently deleting their flow definitions.
  await page.getByRole('button', { name: '流程运行' }).click();
  await page.getByLabel('搜索流程或运行').fill(`${marker}-运行`);
  await expect(page.locator('.run-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: `删除运行 ${marker}-运行-单删` }),
    '永久删除',
  );
  await expect(page.locator('.run-row')).toHaveCount(2);
  await page.getByRole('button', { name: '全选当前结果' }).click();
  await expect(page.getByRole('button', { name: '批量删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    page.getByRole('button', { name: '批量删除 (2)' }),
    '永久删除',
  );
  await expect(page.locator('.run-row')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已永久删除 2 个流程运行');

  await page.getByRole('button', { name: '流程编排' }).click();
  await flowLibrary.getByLabel('搜索流程').fill(`${marker}-流程`);
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(3);
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: `删除流程 ${singleFlow.name}` }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(2);
  await flowLibrary.getByRole('button', { name: '全选' }).click();
  await expect(flowLibrary.getByRole('button', { name: '删除 (2)' })).toBeEnabled();
  await confirmProductDialog(
    page,
    flowLibrary.getByRole('button', { name: '删除 (2)' }),
    '永久删除',
  );
  await expect(flowLibrary.locator('.flow-definition-row')).toHaveCount(0);

  const listed = await request.get(`${apiBase}/api/v1/flow-runs`);
  expect(listed.ok()).toBeTruthy();
  const remainingIds = new Set((await listed.json()).map((run: { id: string }) => run.id));
  for (const run of runs) expect(remainingIds.has(run.id)).toBeFalsy();
  expect(bulkAssetA.id).toBeTruthy();
  expect(bulkAssetB.id).toBeTruthy();
});

test('Agent new-session capability selection is not truncated at 30 items', async ({ page }) => {
  const catalog = Array.from({ length: 31 }, (_, index) => ({
    id: `unlimited-skill-${index}`, capability_type: 'SKILL', capability_key: `unlimited-skill-${index}`,
    description: `能力 ${index}`, filename: `unlimited-skill-${index}.zip`, is_latest: true, document: {},
  }));
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/default')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'draft-capability-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: new Date().toISOString() }) });
      return;
    }
    if (path.endsWith('/runtime')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, message: null, updated_at: new Date().toISOString() }) });
      return;
    }
    if (path.endsWith('/capabilities') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
      return;
    }
    if (path.endsWith('/conversations') || path.endsWith('/work-directories')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: path.endsWith('/conversations') ? '[]' : JSON.stringify({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] }) });
      return;
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(catalog) }));
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([
    { id: 'draft-capability-provider', name: '草稿模型', connection_state: 'CONNECTED', models: [{ model_name: 'draft-model', enabled: true, is_default: true }] },
  ]) }));

  await page.goto('/agent');
  await page.getByRole('button', { name: '新建会话' }).first().click();
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('/');
  const commandMenu = page.getByRole('listbox', { name: '选择 OpenHands 原生能力、命令或 MCP' });
  const pendingCondense = commandMenu.getByRole('option', { name: /压缩上下文/ });
  await expect(pendingCondense).toBeVisible();
  await expect(pendingCondense).toBeDisabled();
  await expect(pendingCondense).toContainText('首条消息创建 OpenHands 原生会话后可调用');
  await expect(commandMenu).toContainText('当前会话还没有加载命令或 MCP');
  await expect(commandMenu.getByRole('button', { name: '管理' })).toBeVisible();
  await commandMenu.getByRole('button', { name: '管理' }).click();
  const manager = page.getByRole('dialog', { name: '能力' });
  await manager.getByRole('button', { name: '选择筛选结果 (31)' }).click();
  await expect(manager).toContainText('已选择 31 项');
});

test('current Agent conversation can select a 31st capability and keeps frozen Context', async ({ page }) => {
  const loadedSkills = Array.from({ length: 29 }, (_, index) => ({
    id: index === 0 ? 'history-skill' : `history-skill-${index}`,
    capability_type: 'SKILL',
    capability_key: index === 0 ? 'history-skill' : `history-skill-${index}`,
    digest: `history-skill-${index}-digest`,
  }));
  const historicalConversation = {
    id: 'history-capability-conversation', workspace_id: 'history-capability-workspace',
    external_conversation_id: 'history-capability-openhands', display_title: '历史会话', title_state: 'FALLBACK', lifecycle: 'ACTIVE',
    working_directory: '/runtime/workspace/project', work_directory_id: null,
    model_provider_id: 'history-provider', model_name: 'history-model', reasoning_effort: null,
    streaming_callback_ready: true, capabilities: [
      ...loadedSkills,
      { id: 'history-context-v1', capability_type: 'CONTEXT', capability_key: '历史上下文', digest: 'history-context-v1-digest' },
    ],
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  };
  const defaultCapabilities = [
    ...loadedSkills,
    { id: 'history-new-skill', capability_type: 'SKILL', capability_key: 'history-new-skill', digest: 'history-new-skill-digest' },
  ];
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/agent-workspaces/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/default')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'history-capability-workspace', display_name: 'Agent 工作区', desired_state: 'RUNNING', updated_at: new Date().toISOString() }) });
      return;
    }
    if (path.endsWith('/runtime')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'ACTIVE', write_available: true, message: null, updated_at: new Date().toISOString() }) });
      return;
    }
    if (path.endsWith('/capabilities') && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(defaultCapabilities) });
      return;
    }
    if (path.endsWith('/capabilities') && request.method() === 'POST') {
      await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE', message: '此历史会话创建时未注册能力市场，无法原地动态加载；请新建会话后继续使用能力。' } }) });
      return;
    }
    if (path.endsWith('/conversations')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([historicalConversation]) });
      return;
    }
    if (path.endsWith('/work-directories')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] }) });
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [], next_cursor: null }) });
      return;
    }
    if (path.endsWith('/context')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ model_name: 'history-model', reasoning_effort: null }) });
      return;
    }
    if (path.endsWith('/pending-confirmation')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: false }) });
      return;
    }
    if (path.endsWith('/workspace')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: { container_id: 'history-runtime' }, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '未配置', note: '' } } }) });
      return;
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }) });
  });
  await page.route('**/api/v1/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([
    ...loadedSkills.map(item => ({ ...item, description: '历史会话迁移验证 Skill', filename: `${item.id}.zip`, is_latest: true, document: {} })),
    { id: 'history-new-skill', capability_type: 'SKILL', capability_key: 'history-new-skill', description: '可追加的历史会话 Skill', filename: 'history-new-skill.zip', is_latest: true, document: {} },
    { id: 'history-context-v1', capability_type: 'CONTEXT', capability_key: '历史上下文', description: '创建时冻结的历史 Context', filename: 'history-context-v1.md', is_latest: false, document: {} },
  ]) }));
  await page.route('**/api/v1/model-providers', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([
    { id: 'history-provider', name: '历史模型', connection_state: 'CONNECTED', models: [{ model_name: 'history-model', enabled: true, is_default: true }] },
  ]) }));

  await page.goto('/agent/conversations/history-capability-conversation');
  const composer = page.getByLabel('发送 Agent 消息');
  await composer.fill('/');
  const commandMenu = page.getByRole('listbox', { name: '选择 OpenHands 原生能力、命令或 MCP' });
  await expect(commandMenu.getByRole('option', { name: /压缩上下文/ })).toBeVisible();
  await expect(commandMenu.getByRole('button', { name: '管理' })).toBeVisible();
  await composer.fill('$');
  const skillMenu = page.getByRole('listbox', { name: '选择技能' });
  await expect(skillMenu.getByRole('option', { name: /^\$history-skill history-skill / })).toBeVisible();
  await expect(skillMenu.getByText('管理当前会话能力')).toBeVisible();
  await skillMenu.getByRole('button', { name: '管理' }).click();
  const manager = page.getByRole('dialog', { name: '能力' });
  await expect(manager).toContainText('已注册 30 项');
  const lockedSkill = manager.getByRole('button', { name: 'history-skill（已注册，不能取消）' });
  await expect(lockedSkill).toBeDisabled();
  await expect(lockedSkill).toHaveClass(/locked/);
  await manager.getByRole('button', { name: /history-new-skill/ }).click();
  await expect(manager).toContainText('已注册 31 项');
  await manager.getByRole('button', { name: 'Context', exact: true }).click();
  await expect(manager).toContainText('已装配 1 个 Context');
  const lockedContext = manager.getByRole('button', { name: '历史上下文（创建时已装配，只读）' });
  await expect(lockedContext).toBeDisabled();
  await expect(lockedContext).toHaveClass(/locked/);
  await expect(manager.getByRole('button', { name: '注册到当前会话' })).toHaveCount(0);
});
