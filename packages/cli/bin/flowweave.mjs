#!/usr/bin/env node

import { chmod, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { basename, dirname, resolve } from 'node:path';
import { homedir } from 'node:os';
import { createInterface } from 'node:readline/promises';
import WebSocket from 'ws';

const API_PREFIX = '/api/v1';
const CONFIG_PATH = process.env.FLOWWEAVE_CONFIG_PATH
  || `${process.env.XDG_CONFIG_HOME || `${homedir()}/.config`}/flowweave/config.json`;
const AUTH_PATH = process.env.FLOWWEAVE_AUTH_PATH || `${dirname(CONFIG_PATH)}/auth.json`;
const SESSION_COOKIE = 'flowweave_session';

class CliError extends Error {}

function usage() {
  return `用法：flowweave <命令> [选项]

配置与发现：
  config init --base-url <URL> [--force]    设置平台基础 URL
  config show                               显示当前配置
  auth <login|status|logout>                 登录、检查或退出平台用户会话
  health [--ready]                          健康检查
  openapi [--paths]                         查看在线 OpenAPI 契约

通用完整接口：
  api <get|post|put|patch|delete> <PATH> [--data JSON|--data-file FILE]
  upload <post|put|patch> <PATH> --file name=FILE [--form name=value]
  ws <PATH> [--message TEXT|--message-json JSON] [--max-messages N]

页面域原子操作：
  node <list|get|create|update|delete> [ID] [--data JSON|--data-file FILE]
  node-directory <list|create|delete|delete-many> [ID] [--id ID ...]
  capability <list|validate|commit|import> ...
  environment <list|get|create|update|delete|setup|publish|stop|version-delete> ...
  credential <list|create|update|delete|delete-many> ...
  flow <list|get|create|update|validate|delete> ...
  run <list|get|start|delete|runtime|replace|pause|resume|cancel|complete|events|node|node-copy|node-delete|workspace-delete|work-directory-delete> ...
  schedule <list|create|pause|resume|trigger|delete> ...
  model <list|create|update|delete|discover|usage|test|oauth-start|oauth-poll|oauth-status|oauth-revoke> ...
  agent <default|workspace|runtime|conversations|conversation|create|send|interrupt|resume|work-directories|work-directory-create|work-directory-delete|file-delete> ...

所有写入操作都可加 --dry-run 仅查看最终请求。运行 flowweave <命令> --help 查看该命令说明。`;
}

function optionValues(args, name) {
  const values = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === name) {
      if (index + 1 >= args.length) throw new CliError(`${name} 缺少值`);
      values.push(args[index + 1]);
      index += 1;
    }
  }
  return values;
}

function option(args, name) {
  return optionValues(args, name).at(-1);
}

function flag(args, name) {
  return args.includes(name);
}

function positional(args) {
  const values = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index].startsWith('--') || args[index] === '-H' || args[index] === '-q') {
      if (!['--dry-run', '--raw', '--ready', '--paths', '--force', '--password-stdin'].includes(args[index])) index += 1;
    } else {
      values.push(args[index]);
    }
  }
  return values;
}

function normalizeBaseUrl(value) {
  let url;
  try { url = new URL(value.trim()); } catch { throw new CliError('--base-url 必须是绝对 HTTP(S) URL'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new CliError('--base-url 只能是无凭据、无查询参数、无片段的 HTTP(S) URL');
  }
  return url.toString().replace(/\/$/, '');
}

async function loadConfig() {
  let data;
  try { data = JSON.parse(await readFile(CONFIG_PATH, 'utf8')); } catch {
    throw new CliError(`尚未配置 FlowWeave。请执行：flowweave config init --base-url <URL>（${CONFIG_PATH}）`);
  }
  if (typeof data?.base_url !== 'string') throw new CliError(`配置 ${CONFIG_PATH} 未定义 base_url`);
  return { baseUrl: normalizeBaseUrl(data.base_url) };
}

async function saveConfig(baseUrl, force) {
  try { await readFile(CONFIG_PATH); if (!force) throw new CliError(`配置已存在于 ${CONFIG_PATH}；传入 --force 可覆盖`); }
  catch (error) { if (error instanceof CliError) throw error; }
  const value = normalizeBaseUrl(baseUrl);
  await mkdir(dirname(CONFIG_PATH), { recursive: true, mode: 0o700 });
  await writeFile(CONFIG_PATH, `${JSON.stringify({ base_url: value }, null, 2)}\n`, { mode: 0o600 });
  return value;
}

async function loadAuth(baseUrl) {
  let data;
  try {
    data = JSON.parse(await readFile(AUTH_PATH, 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw new CliError(`尚未登录 FlowWeave。请执行：flowweave auth login（${AUTH_PATH}）`);
    }
    throw new CliError(`无法读取 FlowWeave 登录会话 ${AUTH_PATH}：${error.message}`);
  }
  if (typeof data?.base_url !== 'string' || typeof data?.session_token !== 'string' || !data.session_token) {
    throw new CliError(`登录会话 ${AUTH_PATH} 格式无效；请重新执行 flowweave auth login`);
  }
  if (normalizeBaseUrl(data.base_url) !== baseUrl) {
    throw new CliError(`登录会话属于其他 FlowWeave 地址；请对 ${baseUrl} 重新执行 flowweave auth login`);
  }
  return data.session_token;
}

async function saveAuth(baseUrl, sessionToken) {
  await mkdir(dirname(AUTH_PATH), { recursive: true, mode: 0o700 });
  await writeFile(AUTH_PATH, `${JSON.stringify({ base_url: baseUrl, session_token: sessionToken }, null, 2)}\n`, { mode: 0o600 });
  await chmod(AUTH_PATH, 0o600);
}

async function clearAuth() {
  await rm(AUTH_PATH, { force: true });
}

function parseJson(value, source) {
  try { return JSON.parse(value); } catch (error) { throw new CliError(`${source} 必须是合法 JSON：${error.message}`); }
}

async function payload(args) {
  const inline = option(args, '--data');
  const file = option(args, '--data-file');
  if (inline && file) throw new CliError('--data 与 --data-file 只能使用其中一个');
  if (file) return parseJson(await readFile(resolve(file), 'utf8'), '--data-file');
  return inline ? parseJson(inline, '--data') : undefined;
}

function pairs(values, separator, label) {
  return values.map((value) => {
    const index = value.indexOf(separator);
    if (index <= 0) throw new CliError(`${label} 必须使用 name${separator}value 形式`);
    return [value.slice(0, index), value.slice(index + 1)];
  });
}

function pathUrl(baseUrl, path, { raw = false, query = [] } = {}) {
  if (!path.startsWith('/') || /^https?:/i.test(path) || path.includes('#')) {
    throw new CliError('PATH 必须是以 / 开头的相对平台路径，不能是完整 URL');
  }
  const url = new URL(baseUrl);
  const apiPath = raw || path.startsWith(`${API_PREFIX}/`) ? path : `${API_PREFIX}${path}`;
  url.pathname = `${url.pathname.replace(/\/$/, '')}${apiPath}`;
  for (const [name, value] of query) url.searchParams.append(name, value);
  return url;
}

function headers(args) { return Object.fromEntries(pairs(optionValues(args, '-H').concat(optionValues(args, '--header')), ':', '--header').map(([name, value]) => [name.trim(), value.trim()])); }
function queries(args) { return pairs(optionValues(args, '-q').concat(optionValues(args, '--query')), '=', '--query'); }

function sessionTokenFrom(response) {
  const values = typeof response.headers.getSetCookie === 'function'
    ? response.headers.getSetCookie()
    : [response.headers.get('set-cookie')].filter(Boolean);
  for (const value of values) {
    const match = new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`).exec(value);
    if (match) return match[1];
  }
  throw new CliError('登录响应没有返回 FlowWeave 会话 Cookie');
}

async function request(method, path, args, {
  raw = false, body, form, query = [], authenticated = true, captureSession = false,
} = {}) {
  const { baseUrl } = await loadConfig();
  const url = pathUrl(baseUrl, path, { raw, query: [...queries(args), ...query] });
  const requestBody = body === undefined ? await payload(args) : body;
  if (flag(args, '--dry-run')) return { method, url: url.toString(), payload: requestBody ?? form ?? null };
  const suppliedHeaders = headers(args);
  if (Object.keys(suppliedHeaders).some((name) => name.toLowerCase() === 'cookie')) {
    throw new CliError('不要用 --header 传入 Cookie；请执行 flowweave auth login');
  }
  const requestHeaders = { Accept: 'application/json', ...suppliedHeaders };
  if (authenticated) requestHeaders.Cookie = `${SESSION_COOKIE}=${await loadAuth(baseUrl)}`;
  let encoded;
  if (form) { encoded = form; } else if (requestBody !== undefined) { encoded = JSON.stringify(requestBody); requestHeaders['Content-Type'] ||= 'application/json'; }
  let response;
  try { response = await fetch(url, { method, headers: requestHeaders, body: encoded }); } catch (error) { throw new CliError(`无法连接 FlowWeave ${url}：${error.message}`); }
  const text = await response.text();
  let value;
  try { value = text ? JSON.parse(text) : { status: response.status }; } catch { value = { status: response.status, body: text }; }
  if (!response.ok) throw new CliError(`HTTP ${response.status}: ${JSON.stringify(value)}`);
  return captureSession ? { value, sessionToken: sessionTokenFrom(response), baseUrl } : value;
}

async function promptText(label) {
  if (!process.stdin.isTTY) throw new CliError(`${label.replace(/[:：]\s*$/, '')} 缺少值`);
  const reader = createInterface({ input: process.stdin, output: process.stderr });
  try { return (await reader.question(label)).trim(); } finally { reader.close(); }
}

async function promptSecret(label) {
  if (!process.stdin.isTTY || typeof process.stdin.setRawMode !== 'function') {
    throw new CliError('非交互环境请使用 --password-stdin 从标准输入传入密码');
  }
  process.stderr.write(label);
  const wasRaw = process.stdin.isRaw;
  process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.setEncoding('utf8');
  return new Promise((resolveSecret, reject) => {
    let value = '';
    const finish = (error) => {
      process.stdin.off('data', onData);
      process.stdin.setRawMode(Boolean(wasRaw));
      process.stdin.pause();
      process.stderr.write('\n');
      if (error) reject(error); else resolveSecret(value);
    };
    const onData = (chunk) => {
      for (const character of chunk) {
        if (character === '\u0003') return finish(new CliError('已取消登录'));
        if (character === '\r' || character === '\n') return finish();
        if (character === '\u007f' || character === '\b') {
          if (value) { value = value.slice(0, -1); process.stderr.write('\b \b'); }
        } else {
          value += character;
          process.stderr.write('•');
        }
      }
    };
    process.stdin.on('data', onData);
  });
}

async function passwordFrom(args) {
  if (!flag(args, '--password-stdin')) return promptSecret('密码：');
  let value = '';
  for await (const chunk of process.stdin) value += String(chunk);
  return value.replace(/\r?\n$/, '');
}

async function auth(args) {
  const [action] = positional(args);
  if (action === 'login') {
    if (flag(args, '--dry-run')) throw new CliError('auth login 不支持 --dry-run，避免密码进入输出');
    const username = (option(args, '--username') || await promptText('用户名：')).trim();
    const password = await passwordFrom(args);
    if (!username || !password) throw new CliError('用户名和密码不能为空');
    const result = await request('POST', '/auth/login', args, {
      body: { username, password }, authenticated: false, captureSession: true,
    });
    await saveAuth(result.baseUrl, result.sessionToken);
    return { authenticated: true, auth_path: AUTH_PATH, user: result.value };
  }
  if (action === 'status') {
    const user = await request('GET', '/auth/me', args);
    return { authenticated: true, auth_path: AUTH_PATH, user };
  }
  if (action === 'logout') {
    await request('POST', '/auth/logout', args, { body: {} });
    await clearAuth();
    return { authenticated: false, auth_path: AUTH_PATH };
  }
  throw new CliError('auth 支持 login|status|logout');
}

async function objectPayload(args, defaults = {}) {
  const value = await payload(args);
  if (value === undefined) return defaults;
  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    throw new CliError('--data 请求体必须是 JSON 对象');
  }
  return { ...value, ...defaults };
}

function openapiPaths(document) {
  if (!document || typeof document !== 'object' || !document.paths || typeof document.paths !== 'object') {
    throw new CliError('OpenAPI 文档不包含 paths 对象');
  }
  const methods = new Set(['get', 'post', 'put', 'patch', 'delete']);
  return Object.entries(document.paths)
    .flatMap(([path, operations]) => (operations && typeof operations === 'object'
      ? Object.keys(operations)
        .filter((method) => methods.has(method.toLowerCase()))
        .map((method) => ({ method: method.toUpperCase(), path }))
      : []))
    .sort((left, right) => left.path.localeCompare(right.path) || left.method.localeCompare(right.method));
}

function print(value) { process.stdout.write(`${JSON.stringify(value, null, 2)}\n`); }

const resourceRoutes = {
  node: '/node-assets',
  'node-directory': '/node-directories',
  flow: '/flows',
  environment: '/terminal-environments',
};

async function crud(command, args) {
  const [action, id] = positional(args);
  const route = resourceRoutes[command];
  const methods = { list: 'GET', get: 'GET', create: 'POST', update: 'PUT', delete: 'DELETE' };
  if (!methods[action]) throw new CliError(`${command} 支持 list|get|create|update|delete`);
  if (['get', 'update', 'delete'].includes(action) && !id) throw new CliError(`${command} ${action} 必须提供 ID`);
  if (['list', 'create'].includes(action) && id) throw new CliError(`${command} ${action} 不接受 ID`);
  return request(methods[action], id ? `${route}/${id}` : route, args);
}

async function nodeDirectory(args) {
  const [action, id] = positional(args);
  if (action === 'list') return request('GET', '/node-directories', args);
  if (action === 'create') return request('POST', '/node-directories', args);
  if (action === 'delete') {
    if (!id) throw new CliError('node-directory delete 必须提供目录 ID');
    return request('DELETE', `/node-directories/${id}`, args);
  }
  if (action === 'delete-many') {
    const ids = optionValues(args, '--id');
    if (!ids.length) throw new CliError('node-directory delete-many 至少需要一个 --id');
    return request('DELETE', '/node-directories', args, { body: { ids } });
  }
  throw new CliError('node-directory 支持 list|create|delete|delete-many');
}

async function capability(args) {
  const [action, id] = positional(args);
  if (action === 'list') return request('GET', '/capabilities', args);
  if (action === 'validate' || action === 'import') {
    const type = option(args, '--type');
    const source = option(args, '--file');
    if (!type || !source) throw new CliError(`capability ${action} 需要 --type 和 --file`);
    const file = resolve(source);
    const content = await readFile(file);
    const body = { capability_type: type, filename: basename(file), content_base64: content.toString('base64') };
    const validated = await request('POST', '/capability-imports/validate', args, { body });
    if (action === 'validate' || flag(args, '--dry-run')) return validated;
    return request('POST', '/capability-imports', args, { body: { import_token: validated.import_token } });
  }
  if (action === 'commit') {
    const token = option(args, '--import-token') || id;
    if (!token) throw new CliError('capability commit 需要 --import-token');
    return request('POST', '/capability-imports', args, { body: { import_token: token } });
  }
  throw new CliError('capability 支持 list|validate|commit|import');
}

async function environment(args) {
  const [action, id] = positional(args);
  if (['list', 'get', 'create', 'update', 'delete'].includes(action)) return crud('environment', args);
  if (action === 'setup') { if (!id) throw new CliError('environment setup 需要环境 ID'); return request('POST', `/terminal-environments/${id}/setup-sessions`, args, { body: await objectPayload(args) }); }
  if (action === 'publish') {
    if (!id) throw new CliError('environment publish 需要 Setup Session ID');
    const description = option(args, '--description');
    return request('POST', `/environment-setup-sessions/${id}/publish`, args, {
      body: await objectPayload(args, description === undefined ? {} : { description }),
    });
  }
  if (action === 'stop') { if (!id) throw new CliError('environment stop 需要 Setup Session ID'); return request('DELETE', `/environment-setup-sessions/${id}`, args); }
  if (action === 'version-delete') {
    const versionId = option(args, '--version');
    if (!id || !versionId) throw new CliError('environment version-delete 需要环境 ID 和 --version');
    return request('DELETE', `/terminal-environments/${id}/versions/${versionId}`, args);
  }
  throw new CliError('environment 支持 list|get|create|update|delete|setup|publish|stop|version-delete');
}

async function credential(args) {
  const [action, id] = positional(args);
  if (action === 'list') return request('GET', '/website-credentials', args);
  if (action === 'create') return request('POST', '/website-credentials', args);
  if (action === 'delete-many') {
    const ids = optionValues(args, '--id');
    if (!ids.length) throw new CliError('credential delete-many 至少需要一个 --id');
    return request('DELETE', '/website-credentials', args, { body: { ids } });
  }
  if (!id) throw new CliError(`credential ${action || ''} 需要认证 ID`);
  if (action === 'update') return request('PUT', `/website-credentials/${id}`, args);
  if (action === 'delete') return request('DELETE', `/website-credentials/${id}`, args);
  throw new CliError('credential 支持 list|create|update|delete|delete-many');
}

async function flow(args) {
  const [action, id] = positional(args);
  if (['list', 'get', 'create', 'update', 'delete'].includes(action)) return crud('flow', args);
  if (action === 'validate') { if (!id) throw new CliError('flow validate 需要流程 ID'); return request('POST', `/flows/${id}/validate`, args, { body: {} }); }
  throw new CliError('flow 支持 list|get|create|update|validate|delete');
}

async function run(args) {
  const [action, id] = positional(args);
  if (action === 'list') return request('GET', '/flow-runs', args);
  if (action === 'start') {
    const flowId = option(args, '--flow'); const environmentVersion = option(args, '--environment-version');
    if (!flowId || !environmentVersion) throw new CliError('run start 需要 --flow 和 --environment-version');
    const body = await objectPayload(args, {
      environment_version_id: environmentVersion,
      ...(option(args, '--name') ? { name: option(args, '--name') } : {}),
    });
    return request('POST', `/flows/${flowId}/runs`, args, { body });
  }
  if (!id) throw new CliError(`run ${action || ''} 需要 FlowRun ID`);
  if (action === 'node' || action === 'node-copy' || action === 'node-delete') {
    const nodeRunId = option(args, '--node');
    if (!nodeRunId) throw new CliError(`run ${action} 需要 --node <node-run-id>`);
    const path = `/flow-runs/${id}/nodes/${nodeRunId}`;
    if (action === 'node') return request('GET', path, args);
    if (action === 'node-copy') return request('POST', `${path}/copy`, args, { body: await objectPayload(args) });
    return request('DELETE', path, args);
  }
  if (action === 'workspace-delete') {
    const attemptId = option(args, '--attempt');
    const paths = optionValues(args, '--path');
    if (!attemptId || !paths.length) {
      throw new CliError('run workspace-delete 需要 FlowRun ID、--attempt 和至少一个 --path');
    }
    const bindingId = option(args, '--binding');
    const workDirectoryId = option(args, '--work-directory');
    return request('DELETE', `/flow-runs/${id}/node-attempts/${attemptId}/agent-sessions/workspace/entries`, args, {
      body: { paths },
      query: [
        ...(bindingId ? [['binding_id', bindingId]] : []),
        ...(workDirectoryId ? [['work_directory_id', workDirectoryId]] : []),
      ],
    });
  }
  if (action === 'work-directory-delete') {
    const attemptId = option(args, '--attempt');
    const workDirectoryId = option(args, '--work-directory');
    if (!attemptId || !workDirectoryId) {
      throw new CliError('run work-directory-delete 需要 FlowRun ID、--attempt 和 --work-directory');
    }
    return request('DELETE', `/flow-runs/${id}/node-attempts/${attemptId}/agent-sessions/work-directories/${workDirectoryId}`, args);
  }
  if (action === 'get') return request('GET', `/flow-runs/${id}`, args);
  if (action === 'delete') return request('DELETE', `/flow-runs/${id}`, args);
  if (action === 'runtime') return request('GET', `/flow-runs/${id}/runtime`, args);
  if (action === 'replace') return request('POST', `/flow-runs/${id}/runtime/replacements`, args);
  if (action === 'pause' || action === 'resume') {
    const body = await objectPayload(args);
    if (!Number.isInteger(body.expected_generation) || !Number.isInteger(body.expected_session_row_version)) {
      throw new CliError(`run ${action} 需要 --data 或 --data-file，且必须包含整数 expected_generation 和 expected_session_row_version`);
    }
    return request('POST', `/flow-runs/${id}/runtime/${action}`, args, { body });
  }
  if (action === 'cancel') return request('POST', `/flow-runs/${id}/cancel`, args, { body: {} });
  if (action === 'complete') return request('POST', `/flow-runs/${id}/complete`, args, { body: {} });
  if (action === 'events') return request('GET', `/flow-runs/${id}/events`, args);
  throw new CliError('run 支持 list|get|start|delete|runtime|replace|pause|resume|cancel|complete|events|node|node-copy|node-delete|workspace-delete|work-directory-delete');
}

async function schedule(args) {
  const [action, id] = positional(args);
  if (action === 'list') {
    if (id) throw new CliError('schedule list 不接受 ID');
    return request('GET', '/flow-run-schedules', args);
  }
  if (action === 'create') {
    if (id) throw new CliError('schedule create 不接受 ID');
    return request('POST', '/flow-run-schedules', args);
  }
  if (!id) throw new CliError(`schedule ${action || ''} 需要调度 ID`);
  if (action === 'pause' || action === 'resume') {
    const expected = Number(option(args, '--expected-row-version'));
    if (!Number.isSafeInteger(expected) || expected < 1) {
      throw new CliError(`schedule ${action} 需要正整数 --expected-row-version`);
    }
    return request('PUT', `/flow-run-schedules/${id}/state`, args, {
      body: { expected_row_version: expected, status: action === 'pause' ? 'PAUSED' : 'ACTIVE' },
    });
  }
  if (action === 'trigger') return request('POST', `/flow-run-schedules/${id}/trigger`, args, { body: {} });
  if (action === 'delete') return request('DELETE', `/flow-run-schedules/${id}`, args);
  throw new CliError('schedule 支持 list|create|pause|resume|trigger|delete');
}

async function model(args) {
  const [action, id] = positional(args);
  if (action === 'list') return request('GET', '/model-providers', args);
  if (action === 'create') return request('POST', '/model-providers', args);
  if (action === 'discover') {
    return id
      ? request('POST', `/model-providers/${id}/discover-models`, args)
      : request('POST', '/model-providers/discover-models', args);
  }
  if (!id) throw new CliError(`model ${action || ''} 需要模型供应商 ID`);
  if (action === 'usage') return request('GET', `/model-providers/${id}/usage`, args);
  if (action === 'update') return request('PUT', `/model-providers/${id}`, args);
  if (action === 'delete') return request('DELETE', `/model-providers/${id}`, args);
  if (action === 'test') return request('POST', `/model-providers/${id}/test`, args);
  if (action === 'oauth-start') return request('POST', `/model-providers/${id}/oauth/device/start`, args, { body: await objectPayload(args) });
  if (action === 'oauth-poll') return request('POST', `/model-providers/${id}/oauth/device/poll`, args, { body: await objectPayload(args) });
  if (action === 'oauth-status') return request('GET', `/model-providers/${id}/oauth/status`, args);
  if (action === 'oauth-revoke') return request('DELETE', `/model-providers/${id}/oauth`, args);
  throw new CliError('model 支持 list|create|update|delete|discover|usage|test|oauth-start|oauth-poll|oauth-status|oauth-revoke');
}

async function agent(args) {
  const [action, workspace, binding] = positional(args);
  if (action === 'default') return request('GET', '/agent-workspaces/default', args);
  if (!workspace) throw new CliError(`agent ${action || ''} 需要 workspace ID`);
  const base = `/agent-workspaces/${workspace}`;
  if (action === 'workspace') return request('GET', base, args);
  if (action === 'runtime') return request('GET', `${base}/runtime`, args);
  if (action === 'conversations') return request('GET', `${base}/conversations`, args);
  if (action === 'work-directories') return request('GET', `${base}/work-directories`, args);
  if (action === 'work-directory-create') {
    return request('POST', `${base}/work-directories`, args);
  }
  if (action === 'work-directory-delete') {
    if (!binding) throw new CliError('agent work-directory-delete 需要工作目录 ID');
    return request('DELETE', `${base}/work-directories/${binding}`, args);
  }
  if (action === 'file-delete') {
    const paths = optionValues(args, '--path');
    if (!paths.length) throw new CliError('agent file-delete 至少需要一个 --path <runtime-path>');
    const bindingId = option(args, '--binding');
    const workDirectoryId = option(args, '--work-directory');
    return request('DELETE', `${base}/workspace/entries`, args, {
      body: { paths },
      query: [
        ...(bindingId ? [['binding_id', bindingId]] : []),
        ...(workDirectoryId ? [['work_directory_id', workDirectoryId]] : []),
      ],
    });
  }
  if (action === 'create') return request('POST', `${base}/conversations`, args);
  if (!binding) throw new CliError(`agent ${action || ''} 需要会话 binding ID`);
  if (action === 'conversation') return request('GET', `${base}/conversations/${binding}`, args);
  if (action === 'send') return request('POST', `${base}/conversations/${binding}/messages`, args);
  if (action === 'interrupt') return request('POST', `${base}/conversations/${binding}/interrupt`, args, { body: {} });
  if (action === 'resume') return request('POST', `${base}/conversations/${binding}/resume`, args, { body: {} });
  throw new CliError('agent 支持 default|workspace|runtime|conversations|conversation|create|send|interrupt|resume|work-directories|work-directory-create|work-directory-delete|file-delete');
}

async function upload(args) {
  const [method, path] = positional(args);
  if (!['post', 'put', 'patch'].includes(method) || !path) throw new CliError('upload 用法：upload <post|put|patch> <PATH> --file name=FILE');
  const fields = pairs(optionValues(args, '--form'), '=', '--form');
  const files = pairs(optionValues(args, '--file'), '=', '--file');
  if (!fields.length && !files.length) throw new CliError('upload 至少需要一个 --form 或 --file 参数');
  const summary = {
    fields: Object.fromEntries(fields),
    files: files.map(([name, file]) => ({ field: name, path: resolve(file), filename: basename(file) })),
  };
  if (flag(args, '--dry-run')) return request(method.toUpperCase(), path, args, { body: summary });
  const form = new FormData();
  for (const [name, value] of fields) form.append(name, value);
  for (const [name, file] of files) form.append(name, new Blob([await readFile(resolve(file))]), basename(file));
  return request(method.toUpperCase(), path, args, { form });
}

async function websocket(args) {
  const [path] = positional(args);
  if (!path) throw new CliError('ws 需要 PATH');
  if (headers(args) && Object.keys(headers(args)).length) {
    throw new CliError('ws 暂不支持自定义请求头；当前 FlowWeave WebSocket 接口无需此项');
  }
  const { baseUrl } = await loadConfig();
  const url = pathUrl(baseUrl, path, { query: queries(args) });
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const maxMessages = Number(option(args, '--max-messages') || 0);
  if (!Number.isInteger(maxMessages) || maxMessages < 0) throw new CliError('--max-messages 必须是大于或等于零的整数');
  let message = option(args, '--message');
  const messageJson = option(args, '--message-json');
  if (message && messageJson) throw new CliError('--message 与 --message-json 只能使用其中一个');
  if (messageJson) message = JSON.stringify(parseJson(messageJson, '--message-json'));
  if (flag(args, '--dry-run')) return { url: url.toString(), message: message || null, max_messages: maxMessages };
  const sessionToken = await loadAuth(baseUrl);
  await new Promise((resolve, reject) => {
    let received = 0;
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve();
    };
    const socket = new WebSocket(url, { headers: { Cookie: `${SESSION_COOKIE}=${sessionToken}` } });
    socket.addEventListener('open', () => { if (message) socket.send(message); });
    socket.addEventListener('message', (event) => {
      const text = String(event.data);
      try { print(JSON.parse(text)); } catch { process.stdout.write(`${text}\n`); }
      received += 1;
      if (maxMessages && received >= maxMessages) socket.close(1000, 'message limit reached');
    });
    socket.addEventListener('error', () => finish(new CliError(`无法连接 FlowWeave WebSocket ${url}`)));
    socket.addEventListener('close', () => finish());
    process.once('SIGINT', () => socket.close(1000, 'interrupted'));
  });
  return { status: 'closed', messages: maxMessages || undefined };
}

async function main(argv) {
  const [command, ...args] = argv;
  if (!command || flag(args, '--help') || command === '--help') { process.stdout.write(`${usage()}\n`); return; }
  let result;
  if (command === 'config') {
    const [action] = positional(args);
    if (action === 'init') { const baseUrl = option(args, '--base-url'); if (!baseUrl) throw new CliError('config init 需要 --base-url'); result = { config_path: CONFIG_PATH, base_url: await saveConfig(baseUrl, flag(args, '--force')) }; }
    else if (action === 'show') { const { baseUrl } = await loadConfig(); result = { config_path: CONFIG_PATH, base_url: baseUrl }; }
    else throw new CliError('config 支持 init|show');
  } else if (command === 'auth') result = auth(args);
  else if (command === 'health') result = request('GET', flag(args, '--ready') ? '/health/ready' : '/health', args, { raw: true, authenticated: false });
  else if (command === 'openapi') { result = request('GET', '/openapi.json', args, { raw: true }); if (flag(args, '--paths')) result = openapiPaths(await result); }
  else if (command === 'api') { const [method, path] = positional(args); if (!method || !path) throw new CliError('api 用法：api <method> <PATH>'); result = request(method.toUpperCase(), path, args, { raw: flag(args, '--raw') }); }
  else if (command === 'upload') result = upload(args);
  else if (command === 'ws') result = websocket(args);
  else if (command === 'node') result = crud(command, args);
  else if (command === 'node-directory') result = nodeDirectory(args);
  else if (command === 'capability') result = capability(args);
  else if (command === 'environment') result = environment(args);
  else if (command === 'credential') result = credential(args);
  else if (command === 'flow') result = flow(args);
  else if (command === 'run') result = run(args);
  else if (command === 'schedule') result = schedule(args);
  else if (command === 'model') result = model(args);
  else if (command === 'agent') result = agent(args);
  else throw new CliError(`未知命令：${command}`);
  print(await result);
}

main(process.argv.slice(2)).catch((error) => { process.stderr.write(`flowweave: error: ${error.message}\n`); process.exitCode = 2; });
