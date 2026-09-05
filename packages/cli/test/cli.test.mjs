import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import test from 'node:test';

const entry = new URL('../bin/flowweave.mjs', import.meta.url);

async function configured() {
  const directory = await mkdtemp(join(tmpdir(), 'flowweave-cli-'));
  return join(directory, 'config.json');
}

function invokeWith(config, args, options = {}) {
  return spawnSync(process.execPath, [entry.pathname, ...args], {
    encoding: 'utf8',
    input: options.input,
    env: { ...process.env, FLOWWEAVE_CONFIG_PATH: config },
  });
}

function invoke(config, ...args) { return invokeWith(config, args); }

async function authServer() {
  const child = spawn(process.execPath, [new URL('./auth-server.mjs', import.meta.url).pathname], {
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const [chunk] = await once(child.stdout, 'data');
  const port = Number(String(chunk).trim());
  assert.ok(Number.isInteger(port) && port > 0);
  return { child, baseUrl: `http://127.0.0.1:${port}/flowweave` };
}

test('配置基础 URL 并显示配置', async () => {
  const config = await configured();
  const initialized = invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  assert.equal(initialized.status, 0, initialized.stderr);
  assert.deepEqual(JSON.parse(initialized.stdout).base_url, 'https://example.test/flowweave');
  assert.deepEqual(JSON.parse(await readFile(config, 'utf8')), { base_url: 'https://example.test/flowweave' });
  const shown = invoke(config, 'config', 'show');
  assert.equal(shown.status, 0, shown.stderr);
  assert.equal(JSON.parse(shown.stdout).config_path, config);
});

test('节点、运行和 WebSocket 原子命令在 dry-run 中生成带部署前缀的地址', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const node = invoke(config, 'node', 'create', '--data', '{"name":"审查"}', '--dry-run');
  assert.equal(node.status, 0, node.stderr);
  assert.deepEqual(JSON.parse(node.stdout), {
    method: 'POST', payload: { name: '审查' }, url: 'https://example.test/flowweave/api/v1/node-assets',
  });
  const run = invoke(config, 'run', 'start', '--flow', 'flow-1', '--environment-version', 'version-1', '--data', '{"request_source":"cli"}', '--dry-run');
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout), {
    method: 'POST',
    payload: { environment_version_id: 'version-1', request_source: 'cli' },
    url: 'https://example.test/flowweave/api/v1/flows/flow-1/runs',
  });
  const websocket = invoke(config, 'ws', '/agent-workspaces/default/runtime/stream', '--max-messages', '1', '--dry-run');
  assert.equal(websocket.status, 0, websocket.stderr);
  assert.equal(JSON.parse(websocket.stdout).url, 'wss://example.test/flowweave/api/v1/agent-workspaces/default/runtime/stream');
});

test('环境配置会话和通用上传 dry-run 保留可读请求体', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const setup = invoke(config, 'environment', 'setup', 'environment-1', '--dry-run');
  assert.equal(setup.status, 0, setup.stderr);
  assert.deepEqual(JSON.parse(setup.stdout).payload, {});
  const upload = invoke(config, 'upload', 'post', '/flow-runs/run-1/artifacts/upload', '--form', 'label=报告', '--file', 'file=./report.txt', '--dry-run');
  assert.equal(upload.status, 0, upload.stderr);
  assert.deepEqual(JSON.parse(upload.stdout).payload.fields, { label: '报告' });
  assert.deepEqual(JSON.parse(upload.stdout).payload.files.map(({ field, filename }) => ({ field, filename })), [{ field: 'file', filename: 'report.txt' }]);
});


test('能力、环境版本、流程与运行原子命令映射正确', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const environment = invoke(config, 'environment', 'version-delete', 'environment-1', '--version', 'version-1', '--dry-run');
  assert.equal(environment.status, 0, environment.stderr);
  assert.equal(JSON.parse(environment.stdout).url, 'https://example.test/flowweave/api/v1/terminal-environments/environment-1/versions/version-1');
  const flow = invoke(config, 'flow', 'validate', 'flow-1', '--dry-run');
  assert.equal(flow.status, 0, flow.stderr);
  assert.equal(JSON.parse(flow.stdout).url, 'https://example.test/flowweave/api/v1/flows/flow-1/validate');
  const events = invoke(config, 'run', 'events', 'run-1', '--dry-run');
  assert.equal(events.status, 0, events.stderr);
  assert.equal(JSON.parse(events.stdout).url, 'https://example.test/flowweave/api/v1/flow-runs/run-1/events');
});

test('维护快捷命令保留删除目标和环境版本说明', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const directory = invoke(config, 'node-directory', 'delete', 'directory-1', '--dry-run');
  assert.equal(directory.status, 0, directory.stderr);
  assert.equal(JSON.parse(directory.stdout).url, 'https://example.test/flowweave/api/v1/node-directories/directory-1');
  const directories = invoke(config, 'node-directory', 'delete-many', '--id', 'directory-1', '--id', 'directory-2', '--dry-run');
  assert.equal(directories.status, 0, directories.stderr);
  assert.deepEqual(JSON.parse(directories.stdout), {
    method: 'DELETE',
    payload: { ids: ['directory-1', 'directory-2'] },
    url: 'https://example.test/flowweave/api/v1/node-directories',
  });
  const credentials = invoke(config, 'credential', 'delete-many', '--id', 'credential-1', '--id', 'credential-2', '--dry-run');
  assert.equal(credentials.status, 0, credentials.stderr);
  assert.deepEqual(JSON.parse(credentials.stdout).payload, { ids: ['credential-1', 'credential-2'] });
  const publish = invoke(config, 'environment', 'publish', 'setup-1', '--description', '更新 Python 依赖', '--dry-run');
  assert.equal(publish.status, 0, publish.stderr);
  assert.deepEqual(JSON.parse(publish.stdout).payload, { description: '更新 Python 依赖' });
  const file = invoke(config, 'agent', 'file-delete', 'workspace-1', '--path', '/runtime/workspace/user-1/report.md', '--path', '/runtime/workspace/user-1/output', '--binding', 'binding-1', '--work-directory', 'directory-1', '--dry-run');
  assert.equal(file.status, 0, file.stderr);
  assert.deepEqual(JSON.parse(file.stdout), {
    method: 'DELETE',
    payload: { paths: ['/runtime/workspace/user-1/report.md', '/runtime/workspace/user-1/output'] },
    url: 'https://example.test/flowweave/api/v1/agent-workspaces/workspace-1/workspace/entries?binding_id=binding-1&work_directory_id=directory-1',
  });
  const runFiles = invoke(config, 'run', 'workspace-delete', 'run-1', '--attempt', 'attempt-1', '--path', '/runtime/workspace/record-1/result.txt', '--path', '/runtime/workspace/record-1/cache', '--binding', 'binding-1', '--work-directory', 'directory-1', '--dry-run');
  assert.equal(runFiles.status, 0, runFiles.stderr);
  assert.deepEqual(JSON.parse(runFiles.stdout), {
    method: 'DELETE',
    payload: { paths: ['/runtime/workspace/record-1/result.txt', '/runtime/workspace/record-1/cache'] },
    url: 'https://example.test/flowweave/api/v1/flow-runs/run-1/node-attempts/attempt-1/agent-sessions/workspace/entries?binding_id=binding-1&work_directory_id=directory-1',
  });
  const runDirectory = invoke(config, 'run', 'work-directory-delete', 'run-1', '--attempt', 'attempt-1', '--work-directory', 'directory-1', '--dry-run');
  assert.equal(runDirectory.status, 0, runDirectory.stderr);
  assert.deepEqual(JSON.parse(runDirectory.stdout), {
    method: 'DELETE',
    payload: null,
    url: 'https://example.test/flowweave/api/v1/flow-runs/run-1/node-attempts/attempt-1/agent-sessions/work-directories/directory-1',
  });
});

test('批量维护命令拒绝缺少精确删除目标', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  for (const args of [
    ['node-directory', 'delete-many', '--dry-run'],
    ['agent', 'file-delete', 'workspace-1', '--dry-run'],
    ['run', 'workspace-delete', 'run-1', '--attempt', 'attempt-1', '--dry-run'],
    ['run', 'work-directory-delete', 'run-1', '--attempt', 'attempt-1', '--dry-run'],
  ]) {
    const result = invoke(config, ...args);
    assert.equal(result.status, 2);
  }
});

test('新增 FlowRun 与模型快捷命令保持公开 API 形状', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const node = invoke(config, 'run', 'node', 'run-1', '--node', 'node-run-1', '--dry-run');
  assert.equal(node.status, 0, node.stderr);
  assert.deepEqual(JSON.parse(node.stdout), {
    method: 'GET', payload: null,
    url: 'https://example.test/flowweave/api/v1/flow-runs/run-1/nodes/node-run-1',
  });
  const copied = invoke(config, 'run', 'node-copy', 'run-1', '--node', 'node-run-1', '--data', '{"name":"再试一次"}', '--dry-run');
  assert.equal(copied.status, 0, copied.stderr);
  assert.deepEqual(JSON.parse(copied.stdout), {
    method: 'POST', payload: { name: '再试一次' },
    url: 'https://example.test/flowweave/api/v1/flow-runs/run-1/nodes/node-run-1/copy',
  });
  const deleted = invoke(config, 'run', 'node-delete', 'run-1', '--node', 'node-run-1', '--dry-run');
  assert.equal(deleted.status, 0, deleted.stderr);
  assert.equal(JSON.parse(deleted.stdout).method, 'DELETE');
  const paused = invoke(config, 'run', 'pause', 'run-1', '--data', '{"expected_generation":2,"expected_session_row_version":9}', '--dry-run');
  assert.equal(paused.status, 0, paused.stderr);
  assert.deepEqual(JSON.parse(paused.stdout), {
    method: 'POST', payload: { expected_generation: 2, expected_session_row_version: 9 },
    url: 'https://example.test/flowweave/api/v1/flow-runs/run-1/runtime/pause',
  });
  const resumed = invoke(config, 'run', 'resume', 'run-1', '--data', '{"expected_generation":2,"expected_session_row_version":10}', '--dry-run');
  assert.equal(resumed.status, 0, resumed.stderr);
  assert.equal(JSON.parse(resumed.stdout).url, 'https://example.test/flowweave/api/v1/flow-runs/run-1/runtime/resume');
  const usage = invoke(config, 'model', 'usage', 'provider-1', '--dry-run');
  assert.equal(usage.status, 0, usage.stderr);
  assert.deepEqual(JSON.parse(usage.stdout), {
    method: 'GET', payload: null,
    url: 'https://example.test/flowweave/api/v1/model-providers/provider-1/usage',
  });
});

test('运行时生命周期命令拒绝缺失或无效 fencing 字段', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  for (const args of [
    ['run', 'pause', 'run-1', '--dry-run'],
    ['run', 'resume', 'run-1', '--data', '{"expected_generation":1}', '--dry-run'],
    ['run', 'pause', 'run-1', '--data', '{"expected_generation":"1","expected_session_row_version":2}', '--dry-run'],
    ['run', 'node', 'run-1', '--dry-run'],
  ]) {
    const result = invoke(config, ...args);
    assert.equal(result.status, 2);
  }
});

test('用户登录会话用于 HTTP 与 WebSocket，退出后从本地清除', async () => {
  const server = await authServer();
  const config = await configured();
  const authPath = join(dirname(config), 'auth.json');
  try {
    assert.equal(invoke(config, 'config', 'init', '--base-url', server.baseUrl).status, 0);
    const rejectedDryRun = invokeWith(config, ['auth', 'login', '--username', 'flowweave', '--password-stdin', '--dry-run'], {
      input: 'correct-password\n',
    });
    assert.equal(rejectedDryRun.status, 2);
    assert.doesNotMatch(`${rejectedDryRun.stdout}${rejectedDryRun.stderr}`, /correct-password/);
    const login = invokeWith(config, ['auth', 'login', '--username', 'flowweave', '--password-stdin'], {
      input: 'correct-password\n',
    });
    assert.equal(login.status, 0, login.stderr);
    assert.equal(JSON.parse(login.stdout).user.username, 'flowweave');
    assert.doesNotMatch(login.stdout, /correct-password|session-secret/);
    assert.deepEqual(JSON.parse(await readFile(authPath, 'utf8')), {
      base_url: server.baseUrl,
      session_token: 'session-secret',
    });
    assert.equal((await stat(authPath)).mode & 0o777, 0o600);

    const status = invoke(config, 'auth', 'status');
    assert.equal(status.status, 0, status.stderr);
    assert.equal(JSON.parse(status.stdout).authenticated, true);
    const api = invoke(config, 'api', 'get', '/flows');
    assert.equal(api.status, 0, api.stderr);
    assert.deepEqual(JSON.parse(api.stdout), [{ id: 'flow-1' }]);
    const websocket = invoke(config, 'ws', '/authenticated-stream', '--max-messages', '1');
    assert.equal(websocket.status, 0, websocket.stderr);
    assert.match(websocket.stdout, /authenticated-websocket/);

    assert.equal(invoke(config, 'config', 'init', '--base-url', `${server.baseUrl}/other`, '--force').status, 0);
    const wrongPlatform = invoke(config, 'auth', 'status');
    assert.equal(wrongPlatform.status, 2);
    assert.match(wrongPlatform.stderr, /属于其他 FlowWeave 地址/);
    assert.doesNotMatch(wrongPlatform.stderr, /session-secret/);
    assert.equal(invoke(config, 'config', 'init', '--base-url', server.baseUrl, '--force').status, 0);

    const logout = invoke(config, 'auth', 'logout');
    assert.equal(logout.status, 0, logout.stderr);
    assert.equal(JSON.parse(logout.stdout).authenticated, false);
    await assert.rejects(readFile(authPath, 'utf8'), { code: 'ENOENT' });
  } finally {
    server.child.kill('SIGTERM');
    await once(server.child, 'exit');
  }
});

test('周期调度快捷命令保持创建、fencing、触发和删除契约', async () => {
  const config = await configured();
  invoke(config, 'config', 'init', '--base-url', 'https://example.test/flowweave');
  const created = invoke(config, 'schedule', 'create', '--data', '{"name":"每小时复核"}', '--dry-run');
  assert.equal(created.status, 0, created.stderr);
  assert.deepEqual(JSON.parse(created.stdout), {
    method: 'POST', payload: { name: '每小时复核' },
    url: 'https://example.test/flowweave/api/v1/flow-run-schedules',
  });
  const paused = invoke(config, 'schedule', 'pause', 'schedule-1', '--expected-row-version', '3', '--dry-run');
  assert.equal(paused.status, 0, paused.stderr);
  assert.deepEqual(JSON.parse(paused.stdout), {
    method: 'PUT', payload: { expected_row_version: 3, status: 'PAUSED' },
    url: 'https://example.test/flowweave/api/v1/flow-run-schedules/schedule-1/state',
  });
  const resumed = invoke(config, 'schedule', 'resume', 'schedule-1', '--expected-row-version', '4', '--dry-run');
  assert.equal(resumed.status, 0, resumed.stderr);
  assert.equal(JSON.parse(resumed.stdout).payload.status, 'ACTIVE');
  const triggered = invoke(config, 'schedule', 'trigger', 'schedule-1', '--dry-run');
  assert.equal(triggered.status, 0, triggered.stderr);
  assert.equal(JSON.parse(triggered.stdout).url, 'https://example.test/flowweave/api/v1/flow-run-schedules/schedule-1/trigger');
  const deleted = invoke(config, 'schedule', 'delete', 'schedule-1', '--dry-run');
  assert.equal(deleted.status, 0, deleted.stderr);
  assert.equal(JSON.parse(deleted.stdout).method, 'DELETE');
  assert.equal(invoke(config, 'schedule', 'pause', 'schedule-1', '--dry-run').status, 2);
});
