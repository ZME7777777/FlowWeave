import { Copy, Plus, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';

type HookEditorMode = 'FORM' | 'JSON';
type HookEvent = 'PreToolUse' | 'PostToolUse' | 'UserPromptSubmit' | 'SessionStart' | 'SessionEnd' | 'Stop';
type HookKind = 'command' | 'script' | 'prompt' | 'agent';
type HookDefinition = {
  type: HookKind; name?: string; command?: string; script?: string; prompt?: string; system_prompt?: string;
  tools?: string[]; timeout?: number; max_iterations?: number; async?: boolean;
};
type HookMatcher = { matcher: string; hooks: HookDefinition[] };
type HookDocument = { name: string; description: string; hooks: Partial<Record<HookEvent, HookMatcher[]>> };
export interface HookScriptAsset { filename: string; contentBase64: string; byteSize: number }

const SCRIPT_EXTENSIONS = new Set(['.sh', '.py', '.js', '.mjs', '.cjs']);
const SCRIPT_MAX_BYTES = 1024 * 1024;
const SCRIPT_MAX_FILES = 20;
const SCRIPT_MAX_TOTAL_BYTES = 10 * 1024 * 1024;

const EVENTS: Array<{ value: HookEvent; label: string; description: string; blocking: boolean }> = [
  { value: 'PreToolUse', label: '工具执行前', description: '在工具真正执行前检查或阻止操作', blocking: true },
  { value: 'PostToolUse', label: '工具执行后', description: '工具执行完成后记录、检查或整理结果', blocking: false },
  { value: 'UserPromptSubmit', label: '提示词提交前', description: '用户消息进入 Agent 前检查内容', blocking: true },
  { value: 'SessionStart', label: '会话开始', description: 'Agent 会话初始化时执行', blocking: false },
  { value: 'SessionEnd', label: '会话结束', description: 'Agent 会话结束时执行清理或审计', blocking: false },
  { value: 'Stop', label: '停止前', description: 'Agent 准备停止前执行最终检查', blocking: true },
];
const EVENT_ALIASES: Record<string, HookEvent> = {
  PreToolUse: 'PreToolUse', pre_tool_use: 'PreToolUse',
  PostToolUse: 'PostToolUse', post_tool_use: 'PostToolUse',
  UserPromptSubmit: 'UserPromptSubmit', user_prompt_submit: 'UserPromptSubmit',
  SessionStart: 'SessionStart', session_start: 'SessionStart',
  SessionEnd: 'SessionEnd', session_end: 'SessionEnd',
  Stop: 'Stop', stop: 'Stop',
};

const FULL_HOOK_EXAMPLE = JSON.stringify({
  name: 'complete-lifecycle-policy',
  description: '覆盖六个生命周期、四种动作类型、多个匹配组与多个动作的完整示例',
  hooks: {
    PreToolUse: [
      {
        matcher: 'terminal',
        hooks: [
          {
            type: 'script',
            name: 'check-terminal-command',
            script: 'check-terminal-command.sh',
            timeout: 30,
          },
          {
            type: 'prompt',
            name: 'review-terminal-risk',
            prompt: '检查本次 terminal 调用是否包含破坏性、越权或泄密风险。仅返回 allow 或 deny，并说明原因。',
            timeout: 60,
          },
        ],
      },
      {
        matcher: '/^(file_editor|terminal)$/',
        hooks: [
          {
            type: 'agent',
            name: 'review-sensitive-change',
            prompt: '审查本次工具调用是否会修改敏感配置或删除重要数据。',
            system_prompt: '你是变更安全审查 Agent。输出 JSON：{"decision":"allow|deny","reason":"..."}。',
            tools: ['file_editor'],
            timeout: 120,
            max_iterations: 5,
          },
        ],
      },
    ],
    PostToolUse: [
      {
        matcher: '*',
        hooks: [
          {
            type: 'command',
            name: 'append-tool-audit',
            command: './hooks/append-tool-audit.sh',
            timeout: 30,
            async: true,
          },
          {
            type: 'prompt',
            name: 'inspect-tool-result',
            prompt: '检查工具结果中是否包含凭据、个人信息或明显失败；给出审计结论。',
            timeout: 60,
          },
        ],
      },
    ],
    UserPromptSubmit: [
      {
        matcher: '*',
        hooks: [
          {
            type: 'prompt',
            name: 'review-user-prompt',
            prompt: '检查用户消息是否请求越权、泄密或破坏性操作。仅返回 allow 或 deny，并说明原因。',
            timeout: 60,
          },
        ],
      },
    ],
    SessionStart: [
      {
        matcher: '*',
        hooks: [
          {
            type: 'command',
            name: 'prepare-session',
            command: './hooks/prepare-session.sh',
            timeout: 45,
          },
        ],
      },
    ],
    SessionEnd: [
      {
        matcher: '*',
        hooks: [
          {
            type: 'command',
            name: 'archive-session-audit',
            command: './hooks/archive-session-audit.sh',
            timeout: 60,
            async: true,
          },
        ],
      },
    ],
    Stop: [
      {
        matcher: '*',
        hooks: [
          {
            type: 'agent',
            name: 'verify-completion',
            prompt: '检查任务是否满足验收条件；未完成时 deny 并指出剩余工作。',
            system_prompt: '你是任务完成度审查 Agent。输出 JSON：{"decision":"allow|deny","reason":"..."}。',
            tools: ['file_editor'],
            timeout: 120,
            max_iterations: 5,
          },
        ],
      },
    ],
  },
}, null, 2);

function emptyAction(): HookDefinition { return { type: 'prompt', prompt: '', timeout: 60 }; }
function emptyMatcher(): HookMatcher { return { matcher: '*', hooks: [emptyAction()] }; }

function parseDocument(text: string): HookDocument {
  let raw: unknown;
  try { raw = JSON.parse(text); } catch { throw new Error('JSON 语法无效，请修正后再切换到表单。'); }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Hook JSON 根节点必须是对象。');
  const root = raw as Record<string, unknown>;
  const rawHooks = root.hooks && typeof root.hooks === 'object' && !Array.isArray(root.hooks)
    ? root.hooks as Record<string, unknown>
    : Object.fromEntries(Object.entries(root).filter(([key]) => Boolean(EVENT_ALIASES[key])));
  const hooks: HookDocument['hooks'] = {};
  for (const [rawEvent, rawMatchers] of Object.entries(rawHooks)) {
    const event = EVENT_ALIASES[rawEvent];
    if (!event) throw new Error(`不支持生命周期事件“${rawEvent}”。`);
    if (!Array.isArray(rawMatchers)) throw new Error(`${event} 必须是 matcher 数组。`);
    hooks[event] = rawMatchers.map((rawMatcher, matcherIndex) => {
      if (!rawMatcher || typeof rawMatcher !== 'object' || Array.isArray(rawMatcher)) throw new Error(`${event} 的第 ${matcherIndex + 1} 个 matcher 无效。`);
      const matcher = rawMatcher as Record<string, unknown>;
      if (!Array.isArray(matcher.hooks)) throw new Error(`${event} 的 matcher 必须包含 hooks 数组。`);
      return {
        matcher: typeof matcher.matcher === 'string' ? matcher.matcher : '*',
        hooks: matcher.hooks.map((rawAction, actionIndex) => {
          if (!rawAction || typeof rawAction !== 'object' || Array.isArray(rawAction)) throw new Error(`${event} 的第 ${actionIndex + 1} 个动作无效。`);
          const action = rawAction as Record<string, unknown>;
          const type = action.type ?? 'command';
          if (type !== 'command' && type !== 'script' && type !== 'prompt' && type !== 'agent') throw new Error(`不支持 Hook 类型“${String(type)}”。`);
          return { ...action, type } as HookDefinition;
        }),
      };
    });
  }
  return {
    name: typeof root.name === 'string' ? root.name : 'hook-policy',
    description: typeof root.description === 'string' ? root.description : '',
    hooks,
  };
}

function serializeDocument(document: HookDocument): string { return JSON.stringify(document, null, 2); }

interface Props {
  json: string; scripts: HookScriptAsset[]; busy: boolean; onJsonChange: (value: string) => void;
  onScriptsChange: (value: HookScriptAsset[]) => void; onClose: () => void; onSave: () => void;
}

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  const chunks: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 0x8000) chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)));
  return btoa(chunks.join(''));
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KiB`;
}

function referencedScripts(document: HookDocument): Set<string> {
  return new Set(Object.values(document.hooks).flatMap(matchers => (matchers ?? []).flatMap(matcher => matcher.hooks.map(action => action.type === 'script' ? action.script ?? '' : '').filter(Boolean))));
}

export function HookEditorDialog({ json, scripts, busy, onJsonChange, onScriptsChange, onClose, onSave }: Props) {
  const [mode, setMode] = useState<HookEditorMode>('FORM');
  const [selectedEvent, setSelectedEvent] = useState<HookEvent>('PreToolUse');
  const [exampleCopied, setExampleCopied] = useState(false);
  const parsed = useMemo(() => {
    try { return { document: parseDocument(json), error: '' }; }
    catch (reason) { return { document: undefined, error: reason instanceof Error ? reason.message : 'Hook JSON 无效。' }; }
  }, [json]);
  const document = parsed.document;
  const eventInfo = EVENTS.find(item => item.value === selectedEvent) ?? EVENTS[0];
  const matchers = document?.hooks[selectedEvent] ?? [];
  const matchesToolName = selectedEvent === 'PreToolUse' || selectedEvent === 'PostToolUse';

  const write = (next: HookDocument, assets: HookScriptAsset[] = scripts) => {
    onJsonChange(serializeDocument(next));
    const referenced = referencedScripts(next);
    onScriptsChange(assets.filter(script => referenced.has(script.filename)));
  };
  const updateDocument = (patch: Partial<Pick<HookDocument, 'name' | 'description'>>) => { if (document) write({ ...document, ...patch }); };
  const updateMatchers = (next: HookMatcher[]) => {
    if (!document) return;
    const hooks = { ...document.hooks };
    if (next.length) hooks[selectedEvent] = next; else delete hooks[selectedEvent];
    write({ ...document, hooks });
  };
  const updateMatcher = (matcherIndex: number, patch: Partial<HookMatcher>) => updateMatchers(matchers.map((item, index) => index === matcherIndex ? { ...item, ...patch } : item));
  const updateAction = (matcherIndex: number, actionIndex: number, patch: Partial<HookDefinition>, removed: Array<keyof HookDefinition> = []) => {
    const matcher = matchers[matcherIndex];
    if (!matcher) return;
    const actions = matcher.hooks.map((item, index) => {
      if (index !== actionIndex) return item;
      const next = { ...item, ...patch };
      removed.forEach(key => delete next[key]);
      return next;
    });
    updateMatcher(matcherIndex, { hooks: actions });
  };
  const uploadScript = async (matcherIndex: number, actionIndex: number, file: File) => {
    const extension = file.name.includes('.') ? `.${file.name.split('.').pop()?.toLowerCase()}` : '';
    if (!SCRIPT_EXTENSIONS.has(extension)) throw new Error(`不支持脚本“${file.name}”；仅支持 .sh、.py、.js、.mjs、.cjs。`);
    if (file.size > SCRIPT_MAX_BYTES) throw new Error(`脚本“${file.name}”不能超过 1 MiB。`);
    const oldName = matchers[matcherIndex]?.hooks[actionIndex]?.script;
    const replacingExisting = scripts.some(script => script.filename === file.name);
    if (!replacingExisting && scripts.length >= SCRIPT_MAX_FILES) throw new Error(`一个 Hook 最多上传 ${SCRIPT_MAX_FILES} 个脚本。`);
    const remaining = scripts.filter(script => script.filename !== file.name);
    const total = remaining.reduce((sum, script) => sum + script.byteSize, 0) + file.size;
    if (total > SCRIPT_MAX_TOTAL_BYTES) throw new Error('Hook 脚本总大小不能超过 10 MiB。');
    const assets = [...remaining, { filename: file.name, byteSize: file.size, contentBase64: toBase64(await file.arrayBuffer()) }];
    const matcher = matchers[matcherIndex];
    if (!document || !matcher) return;
    const nextMatchers = matchers.map((item, index) => index !== matcherIndex ? item : {
      ...item,
      hooks: item.hooks.map((action, index) => index !== actionIndex ? action : { ...action, script: file.name }),
    });
    const next = { ...document, hooks: { ...document.hooks, [selectedEvent]: nextMatchers } };
    const stillReferenced = referencedScripts(next);
    write(next, assets.filter(script => script.filename !== oldName || stillReferenced.has(script.filename)));
  };
  const switchMode = (next: HookEditorMode) => {
    if (next === 'FORM' && !document) return;
    if (next === 'FORM' && document) write(document);
    setMode(next);
  };
  const copyFullExample = () => {
    void navigator.clipboard.writeText(FULL_HOOK_EXAMPLE).then(() => {
      setExampleCopied(true);
      window.setTimeout(() => setExampleCopied(false), 1600);
    });
  };
  const loadFullExample = () => {
    onJsonChange(FULL_HOOK_EXAMPLE);
    setMode('JSON');
  };

  return <div className="modal-backdrop"><section className="modal capability-source-editor hook-editor" role="dialog" aria-modal="true" aria-label="新建 Hook">
    <header><div><span className="eyebrow">NEW HOOK</span><h2>新建生命周期 Hook</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>表单与 JSON 是同一份配置的两种视图。表单修改会立即重写下方同一份 JSON；高级模式的有效 JSON 也可切回表单继续编辑。</p>
    <details className="hook-full-example">
      <summary><span><b>查看全量配置示例</b><small>六个生命周期、四种动作、精确/通配/正则匹配、多动作</small></span></summary>
      <div className="hook-example-actions"><button type="button" className="secondary" onClick={copyFullExample}><Copy size={13}/>{exampleCopied ? '已复制' : '复制 JSON'}</button><button type="button" className="secondary" onClick={loadFullExample}>载入到 JSON 编辑器</button></div>
      <div className="hook-matcher-guide"><b>匹配对象规则</b><span><code>*</code> 匹配所有工具；<code>terminal</code> 精确匹配工具名；<code>/^(terminal|file_editor)$/</code> 使用完整正则匹配。</span><span>只有 PreToolUse / PostToolUse 会传入工具名。UserPromptSubmit、SessionStart、SessionEnd、Stop 必须使用 <code>*</code>。</span></div>
      <pre>{FULL_HOOK_EXAMPLE}</pre>
    </details>
    <div className="mcp-mode-tabs" role="tablist"><button type="button" role="tab" aria-selected={mode === 'FORM'} className={mode === 'FORM' ? 'active' : ''} onClick={() => switchMode('FORM')}>表单配置</button><button type="button" role="tab" aria-selected={mode === 'JSON'} className={mode === 'JSON' ? 'active' : ''} onClick={() => switchMode('JSON')}>JSON 配置</button></div>
    {mode === 'FORM' && document ? <div className="hook-form-view">
      <div className="hook-identity"><label><span>Hook 名称 *</span><input aria-label="Hook 名称" value={document.name} placeholder="例如 protect-dangerous-tools" onChange={event => updateDocument({ name: event.target.value })}/></label><label><span>说明</span><input aria-label="Hook 说明" value={document.description} placeholder="说明策略目的" onChange={event => updateDocument({ description: event.target.value })}/></label></div>
      <div className="hook-event-tabs">{EVENTS.map(item => <button type="button" key={item.value} className={selectedEvent === item.value ? 'active' : ''} onClick={() => setSelectedEvent(item.value)}><b>{item.label}</b><small>{document.hooks[item.value]?.length ?? 0} 组</small></button>)}</div>
      <section className="hook-event-editor"><header><div><b>{eventInfo.label}</b><span>{eventInfo.description}</span></div><button type="button" className="secondary" onClick={() => updateMatchers([...matchers, emptyMatcher()])}><Plus size={13}/>添加匹配组</button></header>
        {eventInfo.blocking && <div className="hook-blocking-note">这是阻断生命周期：动作必须同步执行，可通过结果拒绝后续操作。</div>}
        {matchers.map((matcher, matcherIndex) => <article className="hook-matcher" key={`${selectedEvent}-${matcherIndex}`}><header><label><span>{matchesToolName ? '工具名匹配' : '事件范围'}</span><input aria-label={`${eventInfo.label} 匹配对象 ${matcherIndex + 1}`} value={matcher.matcher} disabled={!matchesToolName} placeholder={matchesToolName ? '*、terminal 或正则' : '*'} onChange={event => updateMatcher(matcherIndex, { matcher: event.target.value })}/><small>{matchesToolName ? '支持精确工具名、* 或 /正则/；匹配的是 OpenHands tool_name。' : '该生命周期没有工具名，只有 * 会命中。'}</small></label><button type="button" className="danger" aria-label="删除匹配组" onClick={() => updateMatchers(matchers.filter((_, index) => index !== matcherIndex))}><Trash2 size={13}/>删除组</button></header>
          <div className="hook-actions">{matcher.hooks.map((action, actionIndex) => <div className="hook-action" key={actionIndex}>
            <label><span>动作类型</span><select aria-label="Hook 动作类型" value={action.type} onChange={event => { const type = event.target.value as HookKind; const removed: Array<keyof HookDefinition> = type === 'command' ? ['script', 'prompt', 'system_prompt', 'tools', 'max_iterations'] : type === 'script' ? ['command', 'script', 'prompt', 'system_prompt', 'tools', 'max_iterations'] : type === 'prompt' ? ['command', 'script', 'system_prompt', 'tools', 'max_iterations'] : ['command', 'script', 'async']; updateAction(matcherIndex, actionIndex, { type }, removed); }}><option value="command">Command · 执行环境命令</option><option value="script">Script · 上传脚本</option><option value="prompt">Prompt · 模型判断</option><option value="agent">Agent · 独立 Agent 检查</option></select></label>
            <label><span>动作名称</span><input value={action.name ?? ''} placeholder="可选" onChange={event => updateAction(matcherIndex, actionIndex, event.target.value ? { name: event.target.value } : {}, event.target.value ? [] : ['name'])}/></label>
            {action.type === 'command' && <label className="wide"><span>命令 *</span><input aria-label="Hook 命令" value={action.command ?? ''} placeholder="flowweave-policy-check" onChange={event => updateAction(matcherIndex, actionIndex, { command: event.target.value })}/><small>命令必须已安装在节点发布的终端环境中。</small></label>}
            {action.type === 'script' && <div className="wide hook-script-upload"><div><b>脚本文件 *</b><span>上传后随 Hook 不可变保存，并在节点工作区安全物化。</span></div><label className="secondary file-button">{action.script ? '替换脚本' : '上传脚本'}<input aria-label="上传 Hook 脚本" type="file" accept=".sh,.py,.js,.mjs,.cjs" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void uploadScript(matcherIndex, actionIndex, file).catch(reason => window.alert(reason instanceof Error ? reason.message : '脚本上传失败')); }}/></label>{action.script && <code>{action.script} · {formatBytes(scripts.find(script => script.filename === action.script)?.byteSize ?? 0)}</code>}</div>}
            {(action.type === 'prompt' || action.type === 'agent') && <label className="wide"><span>{action.type === 'prompt' ? '判断提示词 *' : '任务提示词'}</span><textarea aria-label="Hook 提示词" value={action.prompt ?? ''} placeholder="描述允许、拒绝或检查标准" onChange={event => updateAction(matcherIndex, actionIndex, { prompt: event.target.value })}/></label>}
            {action.type === 'agent' && <><label className="wide"><span>系统提示词</span><textarea value={action.system_prompt ?? ''} onChange={event => updateAction(matcherIndex, actionIndex, event.target.value ? { system_prompt: event.target.value } : {}, event.target.value ? [] : ['system_prompt'])}/></label><label><span>允许工具（每行一个）</span><textarea value={(action.tools ?? []).join('\n')} onChange={event => { const tools = event.target.value.split('\n').map(item => item.trim()).filter(Boolean); updateAction(matcherIndex, actionIndex, tools.length ? { tools } : {}, tools.length ? [] : ['tools']); }}/></label><label><span>最大迭代</span><input type="number" min="1" max="20" value={action.max_iterations ?? 3} onChange={event => updateAction(matcherIndex, actionIndex, { max_iterations: Number(event.target.value) })}/></label></>}
            <label><span>超时（秒）</span><input type="number" min="1" max="300" value={action.timeout ?? 60} onChange={event => updateAction(matcherIndex, actionIndex, { timeout: Number(event.target.value) })}/></label>
            {!eventInfo.blocking && action.type !== 'agent' && <label className="hook-checkbox"><input type="checkbox" checked={Boolean(action.async)} onChange={event => updateAction(matcherIndex, actionIndex, event.target.checked ? { async: true } : {}, event.target.checked ? [] : ['async'])}/><span>异步执行，不阻塞 Agent</span></label>}
            <button type="button" className="ghost hook-remove-action" onClick={() => updateMatcher(matcherIndex, { hooks: matcher.hooks.filter((_, index) => index !== actionIndex) })}><Trash2 size={12}/>删除动作</button>
          </div>)}<button type="button" className="secondary" onClick={() => updateMatcher(matcherIndex, { hooks: [...matcher.hooks, emptyAction()] })}><Plus size={12}/>添加动作</button></div>
        </article>)}
        {!matchers.length && <div className="empty compact">当前生命周期尚未配置。点击“添加匹配组”开始。</div>}
      </section>
    </div> : mode === 'JSON' ? <div className="mcp-json-editor"><textarea aria-label="Hook JSON" value={json} spellCheck={false} onChange={event => onJsonChange(event.target.value)}/><div className="mcp-config-help"><b>Hook JSON 结构</b><span>根节点包含 <code>name</code>、<code>description</code> 与 <code>hooks</code>。</span><span>生命周期支持 PreToolUse、PostToolUse、UserPromptSubmit、SessionStart、SessionEnd、Stop。</span><span>动作支持 command、script、prompt、agent；script 字段填写已上传文件名。</span><span>执行前、提示词提交前与停止前是阻断事件，不能异步。</span></div></div> : null}
    {parsed.error && <p className="error" role="alert">{parsed.error}</p>}
    <div className="mcp-security-note"><b>执行与安全边界</b><span>Hook 与上传脚本随节点进入每次 Attempt 的不可变快照。脚本经哈希校验后物化为只读文件，并转换为 OpenHands 原生 Command Hook；配置禁止包含密钥。</span></div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !document || !document.name.trim() || !Object.keys(document.hooks).length} onClick={onSave}>{busy ? '保存中…' : '校验并保存'}</button></footer>
  </section></div>;
}
