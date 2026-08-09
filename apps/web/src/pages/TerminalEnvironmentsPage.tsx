import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { Box, ClipboardCopy, Play, Plus, Save, Square, Terminal, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, environmentTerminalUrl } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import type { EnvironmentSetupSession, EnvironmentVersion, TerminalEnvironment } from '../types';

function TerminalPanel({ session, onClose }: { session: EnvironmentSetupSession; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [sensitivePaths, setSensitivePaths] = useState<string[]>([]);
  const [cleanupCopied, setCleanupCopied] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const terminalHost = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const blurWhenPointerLeavesTerminal = (event: PointerEvent) => {
      const host = terminalHost.current;
      if (!host || host.contains(event.target as Node)) return;
      if (host.contains(document.activeElement)) {
        (document.activeElement as HTMLElement).blur();
      }
    };
    const closeWhenTerminalIsBlurred = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      const host = terminalHost.current;
      if (host?.contains(document.activeElement)) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    document.addEventListener('pointerdown', blurWhenPointerLeavesTerminal, true);
    document.addEventListener('keydown', closeWhenTerminalIsBlurred, true);
    return () => {
      document.removeEventListener('pointerdown', blurWhenPointerLeavesTerminal, true);
      document.removeEventListener('keydown', closeWhenTerminalIsBlurred, true);
    };
  }, [onClose]);

  useEffect(() => {
    const host = terminalHost.current;
    if (!host) return;
    const term = new XTerm({
      cursorBlink: true, cursorStyle: 'block', convertEol: false, scrollback: 5000,
      fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13, lineHeight: 1.35,
      theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    term.writeln('正在连接隔离终端…');
    let disposed = false;
    let reconnectTimer: number | undefined;
    let attempts = 0;

    const sendResize = () => {
      try { fit.fit(); } catch { return; }
      const ws = socket.current;
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, columns: term.cols }));
      }
    };
    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(environmentTerminalUrl(session.id));
      ws.binaryType = 'arraybuffer';
      socket.current = ws;
      ws.onopen = () => {
        attempts = 0;
        setConnected(true);
        setReconnecting(false);
        setError('');
        term.clear();
        sendResize();
        term.focus();
      };
      ws.onmessage = event => term.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      ws.onerror = () => { /* onclose owns the user-visible retry state. */ };
      ws.onclose = event => {
        if (socket.current === ws) socket.current = null;
        setConnected(false);
        if (disposed || event.code === 1000) return;
        if (event.code === 4404 || event.code === 4409) {
          setReconnecting(false);
          void queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
          onClose();
          return;
        }
        attempts += 1;
        if (attempts <= 5) {
          setReconnecting(true);
          setError(`终端连接中断，正在重连（${attempts}/5）…`);
          reconnectTimer = window.setTimeout(connect, Math.min(1000 * 2 ** (attempts - 1), 8000));
        } else {
          setReconnecting(false);
          setError(event.reason || '终端连接已断开，请关闭视图后重试。');
        }
      };
    };
    const input = term.onData(data => {
      const ws = socket.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data }));
    });
    const resize = term.onResize(({ rows, cols }) => {
      const ws = socket.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', rows, columns: cols }));
    });
    const observer = new ResizeObserver(() => sendResize());
    observer.observe(host);
    connect();
    requestAnimationFrame(sendResize);
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      observer.disconnect();
      input.dispose();
      resize.dispose();
      socket.current?.close(1000);
      socket.current = null;
      term.dispose();
    };
  }, [onClose, queryClient, session.id]);
  const cleanupCommand = sensitivePaths.length
    ? `rm -rf -- ${sensitivePaths.map(path => `'${path.replace(/'/g, `'"'"'`)}'`).join(' ')}`
    : '';
  const publish = async () => {
    setBusy(true); setError(''); setSensitivePaths([]); setCleanupCopied(false);
    try { await api.publishEnvironmentSetup(session.id); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); onClose(); }
    catch (reason) {
      if (reason instanceof ApiError && reason.code === 'ENVIRONMENT_SENSITIVE_FILES_DETECTED') {
        const paths = Array.isArray(reason.details.paths)
          ? reason.details.paths.filter((path): path is string => typeof path === 'string')
          : [];
        setSensitivePaths(paths);
        setError('自动清理后仍检测到敏感文件。请在终端中确认路径后删除并重新发布。');
      } else {
        setError(reason instanceof Error ? reason.message : '发布失败');
      }
    } finally { setBusy(false); }
  };
  const copyCleanupCommand = async () => {
    try {
      await navigator.clipboard.writeText(cleanupCommand);
      setCleanupCopied(true);
    } catch {
      setError('无法访问剪贴板，请手动复制清理命令。');
    }
  };
  const stop = async () => {
    setBusy(true); setError('');
    try { await api.stopEnvironmentSetup(session.id); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); onClose(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '停止失败'); } finally { setBusy(false); }
  };

  return <div className="environment-terminal-backdrop"><section className="environment-terminal-dialog">
    <header><div><span className="eyebrow">ISOLATED SETUP SESSION</span><h2><Terminal size={20}/>环境配置终端</h2><small>{connected ? '已连接 · 所有命令仅作用于隔离容器' : reconnecting ? '连接中断，正在自动重连…' : '连接中…'}</small></div><button className="ghost" onClick={onClose}><X size={16}/>关闭视图</button></header>
    <div ref={terminalHost} className="terminal-screen" aria-label="环境终端，点击后输入命令"/>
    <p className="terminal-help">点击黑色区域后直接输入。支持 Enter、Backspace、方向键、Ctrl+C 和粘贴；终端失焦时按 Esc 关闭视图，聚焦时 Esc 会发送给终端程序。发布前请退出登录并删除 Token、Cookie、SSH Key 等敏感文件。</p>
    {error && <p className="error">{error}</p>}
    {sensitivePaths.length > 0 && <section className="environment-sensitive-files">
      <div><b>阻止发布的路径</b><span>{sensitivePaths.join('、')}</span></div>
      <code>{cleanupCommand}</code>
      <button className="secondary" type="button" onClick={() => void copyCleanupCommand()}><ClipboardCopy size={14}/>{cleanupCopied ? '已复制' : '复制清理命令'}</button>
    </section>}
    <footer><button className="danger" disabled={busy} onClick={() => void stop()}><Square size={14}/>停止并丢弃</button><button className="primary" disabled={busy || !connected} onClick={() => void publish()}><Save size={14}/>{busy ? '处理中…' : '清理并发布版本'}</button></footer>
  </section></div>;
}

export function TerminalEnvironmentsPage() {
  const queryClient = useQueryClient();
  const dialog = useProductDialog();
  const { data: environments = [], isLoading } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments, refetchInterval: 10_000 });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [baseImage, setBaseImage] = useState('flowweave-openhands-runtime:1');
  const [terminal, setTerminal] = useState<EnvironmentSetupSession | null>(null);
  const [error, setError] = useState('');
  const closeTerminal = useCallback(() => setTerminal(null), []);
  useEffect(() => {
    if (!terminal || isLoading) return;
    const stillRunning = environments.some(environment =>
      environment.active_sessions.some(session => session.id === terminal.id),
    );
    if (!stillRunning) setTerminal(null);
  }, [environments, isLoading, terminal]);
  const create = useMutation({ mutationFn: () => api.createTerminalEnvironment({ name: name.trim(), description: description.trim(), base_image: baseImage.trim() }), onSuccess: async () => { setCreating(false); setName(''); setDescription(''); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); }, onError: reason => setError(reason instanceof Error ? reason.message : '创建失败') });
  const open = async (environment: TerminalEnvironment, baseVersionId?: string) => {
    setError('');
    try {
      const current = environment.active_sessions[0];
      setTerminal(current ?? await api.createEnvironmentSetup(environment.id, baseVersionId));
      await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '启动环境失败'); }
  };
  const remove = async (environment: TerminalEnvironment) => {
    const confirmed = await dialog.confirm({
      title: '删除终端环境',
      message: `确定删除“${environment.name}”吗？仍被节点或运行引用时，系统会阻止删除。`,
      confirmLabel: '删除环境',
      tone: 'danger',
    });
    if (!confirmed) return;
    setError('');
    try { await api.deleteTerminalEnvironment(environment.id); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败'); }
  };
  const removeVersion = async (environment: TerminalEnvironment, version: EnvironmentVersion) => {
    if (version.reference_count > 0) return;
    const confirmed = await dialog.confirm({
      title: `删除 ${environment.name} · v${version.version_no}`,
      message: '此操作会删除版本记录并清理对应的本地镜像标签，无法撤销。后续发布仍会使用新的版本号。',
      confirmLabel: '删除版本',
      tone: 'danger',
    });
    if (!confirmed) return;
    setError('');
    try {
      await api.deleteEnvironmentVersion(environment.id, version.id);
      await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'ENVIRONMENT_VERSION_IN_USE') {
        const nodes = Number(reason.details.node_reference_count ?? 0);
        const runs = Number(reason.details.run_reference_count ?? 0);
        setError(`该版本已被占用：${nodes} 个节点、${runs} 个运行。解除引用后才能删除。`);
      } else setError(reason instanceof Error ? reason.message : '删除版本失败');
    }
  };

  return <main className="page environments-page"><header className="page-head"><div><span className="eyebrow">RUNTIME ENVIRONMENTS</span><h1>终端环境管理</h1><p>在隔离容器中交互安装 CLI、系统包与运行库，发布无凭据的不可变环境版本，再由节点选择使用。</p></div><button className="primary" onClick={() => setCreating(true)}><Plus size={15}/>新建环境</button></header>
    <div className="environment-warning"><b>安全边界</b><span>终端拥有隔离容器内的 shell 权限，但不挂载宿主目录。登录凭据不能发布进镜像；正式运行时由凭据代理或短期 Secret 单独注入。</span></div>
    {error && <p className="error">{error}</p>}
    {isLoading ? <div className="empty">加载终端环境…</div> : environments.length ? <div className="environment-grid">{environments.map(environment => { const latest = environment.versions.find(item => item.state === 'READY'); const active = environment.active_sessions[0]; return <article className="environment-card" key={environment.id}>
      <header><span className="environment-icon"><Box size={20}/></span><div><h3>{environment.name}</h3><small>{environment.base_image}</small></div><button className="ghost" aria-label={`删除环境 ${environment.name}`} onClick={() => void remove(environment)}><Trash2 size={15}/></button></header>
      <p>{environment.description || '未填写说明'}</p>
      <dl><div><dt>已发布版本</dt><dd>{environment.versions.filter(item => item.state === 'READY').length}</dd></div><div><dt>最新版本</dt><dd>{latest ? `v${latest.version_no} · ${latest.image_digest.slice(0, 19)}…` : '尚未发布'}</dd></div><div><dt>配置会话</dt><dd>{active ? active.state : '无'}</dd></div></dl>
      {latest?.manifest.commands && <div className="environment-tools">{Object.entries(latest.manifest.commands).slice(0, 8).map(([command, version]) => <span key={command} title={version}>{command}</span>)}</div>}
      {environment.versions.length > 0 && <details className="environment-history"><summary>版本历史（{environment.versions.length}）</summary><div>{environment.versions.map(version => {
        const occupied = version.reference_count > 0;
        return <section key={version.id}>
          <div><b>v{version.version_no}</b><span className={`environment-version-state ${version.state.toLowerCase()}`}>{version.state}</span></div>
          <small>{new Date(version.created_at).toLocaleString()} · {version.image_digest ? `${version.image_digest.slice(0, 19)}…` : '无镜像摘要'}</small>
          <span className={occupied ? 'environment-version-usage occupied' : 'environment-version-usage'}>{occupied ? `${version.node_reference_count} 个节点 · ${version.run_reference_count} 个运行` : '未被占用'}</span>
          <button className="ghost" disabled={occupied} title={occupied ? '解除节点与运行引用后才能删除' : `删除 v${version.version_no}`} aria-label={`删除版本 v${version.version_no}`} onClick={() => void removeVersion(environment, version)}><Trash2 size={14}/></button>
        </section>;
      })}</div></details>}
      <footer>{active ? <button className="primary" onClick={() => setTerminal(active)}><Terminal size={14}/>继续配置</button> : <><button className="secondary" onClick={() => void open(environment, latest?.id)}><Play size={14}/>{latest ? `从 v${latest.version_no} 创建草稿` : '开启终端'}</button></>}</footer>
    </article>; })}</div> : <div className="empty">暂无终端环境。新建后可在隔离终端中安装节点需要的命令。</div>}
    {creating && <div className="modal-backdrop"><form className="modal editor environment-create-dialog" onSubmit={event => { event.preventDefault(); setError(''); create.mutate(); }}><header><div><span className="eyebrow">NEW ENVIRONMENT</span><h2>新建终端环境</h2></div><button type="button" className="ghost" onClick={() => setCreating(false)}>关闭</button></header><section className="form-grid form-pane"><label>名称<input required maxLength={200} value={name} onChange={event => setName(event.target.value)}/></label><label>基础镜像<input required value={baseImage} onChange={event => setBaseImage(event.target.value)}/></label><label className="wide">说明<textarea value={description} onChange={event => setDescription(event.target.value)}/></label></section><footer><button type="button" className="ghost" onClick={() => setCreating(false)}>取消</button><button className="primary" disabled={create.isPending}>{create.isPending ? '创建中…' : '创建环境'}</button></footer></form></div>}
    {terminal && <TerminalPanel session={terminal} onClose={closeTerminal}/>}
  </main>;
}
