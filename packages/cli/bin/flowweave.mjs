#!/usr/bin/env node

import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, resolve } from 'node:path';
import { homedir } from 'node:os';

const API_PREFIX = '/api/v1';
const CONFIG_PATH = process.env.FLOWWEAVE_CONFIG_PATH
  || `${process.env.XDG_CONFIG_HOME || `${homedir()}/.config`}/flowweave/config.json`;

class CliError extends Error {}

function usage() {
  return `用法：flowweave <命令> [选项]

配置与发现：
  config init --base-url <URL> [--force]    设置平台基础 URL（无需登录）
  config show                               显示当前配置
  health [--ready]                          健康检查
  openapi [--paths]                         查看在线 OpenAPI 契约

通用完整接口：
  api <get|post|put|patch|delete> <PATH> [--data JSON|--data-file FILE]
  upload <post|put|patch> <PATH> --file name=FILE [--form name=value]
  ws <PATH> [--message TEXT|--message-json JSON] [--max-messages N]

页面域原子操作：
  node <list|get|create|update|delete> [ID] [--data JSON|--data-file FILE]
  node-directory <list|create> [--data JSON|--data-file FILE]
  capability <list|validate|commit|import> ...
  environment <list|get|create|update|delete|setup|publish|stop|version-delete> ...
  flow <list|get|create|update|validate|delete> ...
  run <list|get|start|delete|runtime|replace|cancel|complete|events> ...
  model <list|create|update|delete|discover|test|oauth-start|oauth-poll|oauth-status|oauth-revoke> ...
  agent <default|workspace|runtime|conversations|conversation|create|send|interrupt|resume> ...

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
      if (!['--dry-run', '--raw', '--ready', '--paths', '--force'].includes(args[index])) index += 1;
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

async function request(method, path, args, { raw = false, body, form } = {}) {
  const { baseUrl } = await loadConfig();
  const url = pathUrl(baseUrl, path, { raw, query: queries(args) });
  const requestBody = body === undefined ? await payload(args) : body;
  if (flag(args, '--dry-run')) return { method, url: url.toString(), payload: requestBody ?? form ?? null };
  const requestHeaders = { Accept: 'application/json', ...headers(args) };
  let encoded;
  if (form) { encoded = form; } else if (requestBody !== undefined) { encoded = JSON.stringify(requestBody); requestHeaders['Content-Type'] ||= 'application/json'; }
  let response;
  try { response = await fetch(url, { method, headers: requestHeaders, body: encoded }); } catch (error) { throw new CliError(`无法连接 FlowWeave ${url}：${error.message}`); }
  const text = await response.text();
  let value;
  try { value = text ? JSON.parse(text) : { status: response.status }; } catch { value = { status: response.status, body: text }; }
  if (!response.ok) throw new CliError(`HTTP ${response.status}: ${JSON.stringify(value)}`);
  return value;
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
  if (action === 'publish') { if (!id) throw new CliError('environment publish 需要 Setup Session ID'); return request('POST', `/environment-setup-sessions/${id}/publish`, args, { body: {} }); }
  if (action === 'stop') { if (!id) throw new CliError('environment stop 需要 Setup Session ID'); return request('DELETE', `/environment-setup-sessions/${id}`, args); }
  if (action === 'version-delete') {
    const versionId = option(args, '--version');
    if (!id || !versionId) throw new CliError('environment version-delete 需要环境 ID 和 --version');
    return request('DELETE', `/terminal-environments/${id}/versions/${versionId}`, args);
  }
  throw new CliError('environment 支持 list|get|create|update|delete|setup|publish|stop|version-delete');
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
  if (action === 'get') return request('GET', `/flow-runs/${id}`, args);
  if (action === 'delete') return request('DELETE', `/flow-runs/${id}`, args);
  if (action === 'runtime') return request('GET', `/flow-runs/${id}/runtime`, args);
  if (action === 'replace') return request('POST', `/flow-runs/${id}/runtime/replacements`, args);
  if (action === 'cancel') return request('POST', `/flow-runs/${id}/cancel`, args, { body: {} });
  if (action === 'complete') return request('POST', `/flow-runs/${id}/complete`, args, { body: {} });
  if (action === 'events') return request('GET', `/flow-runs/${id}/events`, args);
  throw new CliError('run 支持 list|get|start|delete|runtime|replace|cancel|complete|events');
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
  if (action === 'update') return request('PUT', `/model-providers/${id}`, args);
  if (action === 'delete') return request('DELETE', `/model-providers/${id}`, args);
  if (action === 'test') return request('POST', `/model-providers/${id}/test`, args);
  if (action === 'oauth-start') return request('POST', `/model-providers/${id}/oauth/device/start`, args, { body: await objectPayload(args) });
  if (action === 'oauth-poll') return request('POST', `/model-providers/${id}/oauth/device/poll`, args, { body: await objectPayload(args) });
  if (action === 'oauth-status') return request('GET', `/model-providers/${id}/oauth/status`, args);
  if (action === 'oauth-revoke') return request('DELETE', `/model-providers/${id}/oauth`, args);
  throw new CliError('model 支持 list|create|update|delete|discover|test|oauth-start|oauth-poll|oauth-status|oauth-revoke');
}

async function agent(args) {
  const [action, workspace, binding] = positional(args);
  if (action === 'default') return request('GET', '/agent-workspaces/default', args);
  if (!workspace) throw new CliError(`agent ${action || ''} 需要 workspace ID`);
  const base = `/agent-workspaces/${workspace}`;
  if (action === 'workspace') return request('GET', base, args);
  if (action === 'runtime') return request('GET', `${base}/runtime`, args);
  if (action === 'conversations') return request('GET', `${base}/conversations`, args);
  if (action === 'create') return request('POST', `${base}/conversations`, args);
  if (!binding) throw new CliError(`agent ${action || ''} 需要会话 binding ID`);
  if (action === 'conversation') return request('GET', `${base}/conversations/${binding}`, args);
  if (action === 'send') return request('POST', `${base}/conversations/${binding}/messages`, args);
  if (action === 'interrupt') return request('POST', `${base}/conversations/${binding}/interrupt`, args, { body: {} });
  if (action === 'resume') return request('POST', `${base}/conversations/${binding}/resume`, args, { body: {} });
  throw new CliError('agent 支持 default|workspace|runtime|conversations|conversation|create|send|interrupt|resume');
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
  await new Promise((resolve, reject) => {
    let received = 0;
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve();
    };
    const socket = new WebSocket(url);
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
  } else if (command === 'health') result = request('GET', flag(args, '--ready') ? '/health/ready' : '/health', args, { raw: true });
  else if (command === 'openapi') { result = request('GET', '/openapi.json', args, { raw: true }); if (flag(args, '--paths')) result = openapiPaths(await result); }
  else if (command === 'api') { const [method, path] = positional(args); if (!method || !path) throw new CliError('api 用法：api <method> <PATH>'); result = request(method.toUpperCase(), path, args, { raw: flag(args, '--raw') }); }
  else if (command === 'upload') result = upload(args);
  else if (command === 'ws') result = websocket(args);
  else if (['node', 'node-directory'].includes(command)) result = crud(command, args);
  else if (command === 'capability') result = capability(args);
  else if (command === 'environment') result = environment(args);
  else if (command === 'flow') result = flow(args);
  else if (command === 'run') result = run(args);
  else if (command === 'model') result = model(args);
  else if (command === 'agent') result = agent(args);
  else throw new CliError(`未知命令：${command}`);
  print(await result);
}

main(process.argv.slice(2)).catch((error) => { process.stderr.write(`flowweave: error: ${error.message}\n`); process.exitCode = 2; });
