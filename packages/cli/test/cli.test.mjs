import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

const entry = new URL('../bin/flowweave.mjs', import.meta.url);

async function configured() {
  const directory = await mkdtemp(join(tmpdir(), 'flowweave-cli-'));
  return join(directory, 'config.json');
}

function invoke(config, ...args) {
  return spawnSync(process.execPath, [entry.pathname, ...args], {
    encoding: 'utf8',
    env: { ...process.env, FLOWWEAVE_CONFIG_PATH: config },
  });
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
  const file = invoke(config, 'agent', 'file-delete', 'workspace-1', '--path', '/runtime/workspace/project/report.md', '--path', '/runtime/workspace/project/output', '--binding', 'binding-1', '--work-directory', 'directory-1', '--dry-run');
  assert.equal(file.status, 0, file.stderr);
  assert.deepEqual(JSON.parse(file.stdout), {
    method: 'DELETE',
    payload: { paths: ['/runtime/workspace/project/report.md', '/runtime/workspace/project/output'] },
    url: 'https://example.test/flowweave/api/v1/agent-workspaces/workspace-1/workspace/entries?binding_id=binding-1&work_directory_id=directory-1',
  });
  const runFiles = invoke(config, 'run', 'workspace-delete', 'run-1', '--attempt', 'attempt-1', '--path', '/runtime/workspace/project/result.txt', '--path', '/runtime/workspace/project/cache', '--binding', 'binding-1', '--work-directory', 'directory-1', '--dry-run');
  assert.equal(runFiles.status, 0, runFiles.stderr);
  assert.deepEqual(JSON.parse(runFiles.stdout), {
    method: 'DELETE',
    payload: { paths: ['/runtime/workspace/project/result.txt', '/runtime/workspace/project/cache'] },
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
