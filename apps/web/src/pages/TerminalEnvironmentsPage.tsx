import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { Box, LoaderCircle, Play, Plus, Save, Square, Terminal, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, environmentTerminalUrl } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import type { EnvironmentSetupSession, EnvironmentVersion, TerminalEnvironment } from '../types';

function PublishingPanel({ onClose }: { onClose: () => void }) {
  return <div className="environment-terminal-backdrop"><section className="environment-terminal-dialog">
    <header><div><span className="eyebrow">ENVIRONMENT PUBLISHING</span><h2><LoaderCircle className="spin" size={20}/>正在发布环境版本</h2><small>已冻结配置终端，正在后台构建并验证 Runtime 镜像</small></div><button className="ghost" title="仅关闭当前窗口，发布会继续进行" onClick={onClose}><X size={16}/>关闭视图</button></header>
    <div className="environment-publishing-status"><LoaderCircle className="spin" size={28}/><div><b>环境版本正在后台发布</b><p>这一步会提交配置容器、通过 OpenHands 正式构建链打包 Runtime，并执行镜像契约探针。完成前不能重新连接或停止此终端。</p></div></div>
    <footer><button className="primary" onClick={onClose}>后台继续发布</button></footer>
  </section></div>;
}

function TerminalPanel({ session, publishError, onClose, onPublishing, onPublishFailed }: { session: EnvironmentSetupSession; publishError: string; onClose: () => void; onPublishing: () => void; onPublishFailed: (message: string) => void }) {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
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
    let resizeFrame: number | undefined;
    let attempts = 0;
    let connectionStarted = false;

    const sendResize = () => {
      if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = undefined;
        if (disposed || host.clientWidth < 160 || host.clientHeight < 100) return;
        let dimensions: { cols: number; rows: number } | undefined;
        try { dimensions = fit.proposeDimensions(); } catch { return; }
        // FitAddon falls back to two columns when its parent is measured before
        // the dialog's flex layout has settled. Never propagate that transient
        // size to xterm or the remote PTY.
        if (!dimensions || dimensions.cols < 20 || dimensions.rows < 2) return;
        if (term.cols !== dimensions.cols || term.rows !== dimensions.rows) {
          term.resize(dimensions.cols, dimensions.rows);
        }
        if (!connectionStarted) {
          connectionStarted = true;
          connect();
          return;
        }
        const ws = socket.current;
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: dimensions.rows, columns: dimensions.cols }));
        }
      });
    };
    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(environmentTerminalUrl(session.id, term.rows, term.cols));
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
    const observer = new ResizeObserver(() => sendResize());
    observer.observe(host);
    // Do not create the persistent tmux client with xterm's 80x24 defaults.
    // The first valid fit above establishes both the browser and remote PTY
    // dimensions before tmux paints its initial screen.
    sendResize();
    void document.fonts?.ready.then(sendResize);
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame);
      observer.disconnect();
      input.dispose();
      socket.current?.close(1000);
      socket.current = null;
      term.dispose();
    };
  }, [onClose, queryClient, session.id]);
  const publish = async () => {
    setBusy(true); setError('');
    onPublishing();
    try { await api.publishEnvironmentSetup(session.id); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); onClose(); }
    catch (reason) { onPublishFailed(reason instanceof Error ? reason.message : '发布失败'); }
    finally { setBusy(false); }
  };
  const stop = async () => {
    setBusy(true); setError('');
    try { await api.stopEnvironmentSetup(session.id); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); onClose(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '停止失败'); } finally { setBusy(false); }
  };

  return <div className="environment-terminal-backdrop"><section className="environment-terminal-dialog">
    <header><div><span className="eyebrow">ISOLATED SETUP SESSION</span><h2><Terminal size={20}/>环境配置终端</h2><small>{connected ? '已连接 · 关闭视图后任务仍会继续运行' : reconnecting ? '连接中断，正在自动重连…' : '连接中…'}</small></div><button className="ghost" title="仅关闭当前窗口，不会终止终端任务" onClick={onClose}><X size={16}/>关闭视图</button></header>
    <div className="terminal-screen"><div ref={terminalHost} className="terminal-screen-body" aria-label="环境终端，点击后输入命令"/></div>
    <p className="terminal-help">点击黑色区域后直接输入。关闭视图只会断开当前窗口，终端及其中的任务会继续运行；再次点击“继续配置”将返回同一个终端。支持 Enter、Backspace、方向键、Ctrl+C 和粘贴；终端失焦时按 Esc 关闭视图，聚焦时 Esc 会发送给终端程序。发布会保留容器文件系统中的认证信息、缓存和命令历史，请仅在受信任环境中使用和分发镜像。</p>
    {(error || publishError) && <p className="error">{error || publishError}</p>}
    <footer><button className="danger" disabled={busy} onClick={() => void stop()}><Square size={14}/>停止并丢弃</button><button className="primary" disabled={busy || !connected} onClick={() => void publish()}><Save size={14}/>{busy ? '处理中…' : '发布环境版本'}</button></footer>
  </section></div>;
}

export function TerminalEnvironmentsPage() {
  const queryClient = useQueryClient();
  const dialog = useProductDialog();
  const { data: environments = [], isLoading } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments, refetchInterval: 10_000 });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [terminal, setTerminal] = useState<EnvironmentSetupSession | null>(null);
  const [terminalError, setTerminalError] = useState('');
  const [openingEnvironmentId, setOpeningEnvironmentId] = useState<string | null>(null);
  const [deletingEnvironmentId, setDeletingEnvironmentId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const closeTerminal = useCallback(() => { setTerminal(null); setTerminalError(''); }, []);
  useEffect(() => {
    if (!terminal || isLoading) return;
    const current = environments.flatMap(environment => environment.active_sessions).find(session => session.id === terminal.id);
    if (!current) { setTerminal(null); setTerminalError(''); return; }
    if (current.state !== terminal.state || current.error_detail !== terminal.error_detail) setTerminal(current);
  }, [environments, isLoading, terminal]);
  const create = useMutation({ mutationFn: () => api.createTerminalEnvironment({ name: name.trim(), description: description.trim() }), onSuccess: async () => { setCreating(false); setName(''); setDescription(''); await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); }, onError: reason => setError(reason instanceof Error ? reason.message : '创建失败') });
  const open = async (environment: TerminalEnvironment, baseVersionId?: string) => {
    if (openingEnvironmentId) return;
    setOpeningEnvironmentId(environment.id);
    setError('');
    try {
      const current = environment.active_sessions[0];
      setTerminalError('');
      setTerminal(current ?? await api.createEnvironmentSetup(environment.id, baseVersionId));
      await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '启动环境失败');
    } finally {
      setOpeningEnvironmentId(null);
    }
  };
  const explainEnvironmentInUse = (environment: TerminalEnvironment, reason: ApiError) => {
    const runs = Number(reason.details.flow_run_reference_count ?? 0);
    const snapshots = Number(reason.details.snapshot_reference_count ?? 0);
    const references = [
      runs > 0 ? `${runs} 个流程运行` : '',
      snapshots > 0 ? `${snapshots} 份冻结快照` : '',
    ].filter(Boolean);
    const message = references.length
      ? `无法删除终端环境：“${environment.name}”的基础镜像版本仍被${references.join('和')}引用。为保证这些运行可复现，请先永久删除相关流程运行；其冻结快照清理后即可再试。配置终端不会被停止。`
      : `无法删除终端环境：“${environment.name}”仍被受保护的记录引用。请先解除引用后再试；配置终端不会被停止。`;
    setError(message);
  };
  const remove = async (environment: TerminalEnvironment) => {
    if (environment.active_sessions.some(session => session.state === 'PUBLISHING')) {
      setError('环境版本正在发布，请等待完成或失败后再删除环境。');
      return;
    }
    const confirmed = await dialog.confirm({
      title: '删除终端环境',
      message: environment.active_sessions.length
        ? `确定删除“${environment.name}”吗？未发布的配置会话会停止并丢弃；仍被流程运行引用时，系统会阻止删除。`
        : `确定删除“${environment.name}”吗？仍被流程运行引用时，系统会阻止删除。`,
      confirmLabel: '删除环境',
      tone: 'danger',
    });
    if (!confirmed) return;
    setError('');
    setDeletingEnvironmentId(environment.id);
    try {
      try {
        await api.deleteTerminalEnvironment(environment.id);
        await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
        return;
      } catch (reason) {
        if (!(reason instanceof ApiError) || reason.code !== 'ENVIRONMENT_SETUP_ACTIVE') {
          if (reason instanceof ApiError && reason.code === 'ENVIRONMENT_IN_USE') {
            explainEnvironmentInUse(environment, reason);
            return;
          }
          throw reason;
        }
      }
      if (terminal?.environment_id === environment.id) closeTerminal();
      for (const session of environment.active_sessions) await api.stopEnvironmentSetup(session.id);
      for (let attempt = 0; attempt < 40; attempt += 1) {
        try {
          await api.deleteTerminalEnvironment(environment.id);
          await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
          return;
        } catch (reason) {
          if (!(reason instanceof ApiError) || reason.code !== 'ENVIRONMENT_SETUP_ACTIVE') {
            if (reason instanceof ApiError && reason.code === 'ENVIRONMENT_IN_USE') {
              explainEnvironmentInUse(environment, reason);
              return;
            }
            throw reason;
          }
          await new Promise<void>(resolve => window.setTimeout(resolve, 500));
        }
      }
      throw new Error('配置终端仍在回收，请稍后重试删除。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
      await queryClient.invalidateQueries({ queryKey: ['terminal-environments'] });
    } finally {
      setDeletingEnvironmentId(null);
    }
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
        const runs = Number(reason.details.run_reference_count ?? 0);
        setError(`该版本已被 ${runs} 个运行占用。解除引用后才能删除。`);
      } else setError(reason instanceof Error ? reason.message : '删除版本失败');
    }
  };

  return <main className="page environments-page"><header className="page-head"><div><span className="eyebrow">RUNTIME ENVIRONMENTS</span><h1>终端环境管理</h1><p>在隔离容器中交互安装 CLI、系统包与运行库，发布不可变环境版本，并在创建流程运行时选择使用。</p></div><button className="primary" onClick={() => setCreating(true)}><Plus size={15}/>新建环境</button></header>
    <div className="environment-warning"><b>凭据风险</b><span>终端不挂载宿主目录，但发布不会清理或拒绝认证文件。镜像可能永久包含 Token、密钥、Cookie 和命令历史，请限制镜像访问与分发范围。</span></div>
    {error && <p className="error">{error}</p>}
    {isLoading ? <div className="empty">加载终端环境…</div> : environments.length ? <div className="environment-grid">{environments.map(environment => { const latest = environment.versions.find(item => item.state === 'READY'); const latestCompatible = environment.versions.find(item => item.state === 'READY' && item.runtime_compatible); const active = environment.active_sessions[0]; return <article className="environment-card" key={environment.id}>
      <header><span className="environment-icon"><Box size={20}/></span><div><h3>{environment.name}</h3></div><button className="ghost" disabled={deletingEnvironmentId !== null} aria-label={`删除环境 ${environment.name}`} onClick={() => void remove(environment)}>{deletingEnvironmentId === environment.id ? <LoaderCircle className="spin" size={15}/> : <Trash2 size={15}/>}</button></header>
      <p>{environment.description || '未填写说明'}</p>
      <dl><div><dt>可运行版本</dt><dd>{environment.versions.filter(item => item.state === 'READY' && item.runtime_compatible).length}</dd></div><div><dt>最新可运行版本</dt><dd>{latestCompatible ? `v${latestCompatible.version_no} · ${latestCompatible.image_digest.slice(0, 19)}…` : '需要重新发布'}</dd></div><div><dt>配置会话</dt><dd>{active ? active.state : '无'}</dd></div></dl>
      {latest?.manifest.commands && <div className="environment-tools">{Object.entries(latest.manifest.commands).slice(0, 8).map(([command, version]) => <span key={command} title={version}>{command}</span>)}</div>}
      {environment.versions.length > 0 && <details className="environment-history"><summary>版本历史（{environment.versions.length}）</summary><div>{environment.versions.map(version => {
        const occupied = version.reference_count > 0;
        return <section key={version.id}>
          <div><b>v{version.version_no}</b><span className={`environment-version-state ${version.state.toLowerCase()}`}>{version.state}</span></div>
          <small>{new Date(version.created_at).toLocaleString()} · {version.image_digest ? `${version.image_digest.slice(0, 19)}…` : '无镜像摘要'}</small>
          <span className={occupied ? 'environment-version-usage occupied' : 'environment-version-usage'}>{version.state === 'READY' && !version.runtime_compatible ? '缺少运行契约，需重新发布' : occupied ? `${version.run_reference_count} 个运行` : '未被占用'}</span>
          <button className="ghost" disabled={occupied} title={occupied ? '解除运行引用后才能删除' : `删除 v${version.version_no}`} aria-label={`删除版本 v${version.version_no}`} onClick={() => void removeVersion(environment, version)}><Trash2 size={14}/></button>
        </section>;
      })}</div></details>}
      <footer>{active ? <button className="primary" disabled={deletingEnvironmentId !== null} onClick={() => setTerminal(active)}>{active.state === 'PUBLISHING' ? <LoaderCircle className="spin" size={14}/> : <Terminal size={14}/>}{active.state === 'PUBLISHING' ? '查看发布进度' : '继续配置'}</button> : <button className="secondary" disabled={openingEnvironmentId !== null || deletingEnvironmentId !== null} aria-busy={openingEnvironmentId === environment.id} onClick={() => void open(environment, latest?.id)}>{openingEnvironmentId === environment.id ? <LoaderCircle className="spin" size={14}/> : <Play size={14}/>}<span aria-live="polite">{openingEnvironmentId === environment.id ? (latest ? '正在创建草稿…' : '正在开启终端…') : latest ? `从 v${latest.version_no} 创建草稿` : '开启终端'}</span></button>}</footer>
    </article>; })}</div> : <div className="empty">暂无终端环境。新建后可在隔离终端中安装节点需要的命令。</div>}
    {creating && <div className="modal-backdrop"><form className="modal editor environment-create-dialog" onSubmit={event => { event.preventDefault(); setError(''); create.mutate(); }}><header><div><span className="eyebrow">NEW ENVIRONMENT</span><h2>新建终端环境</h2></div><button type="button" className="ghost" onClick={() => setCreating(false)}>关闭</button></header><section className="form-grid form-pane"><label className="wide">名称<input required maxLength={200} value={name} onChange={event => setName(event.target.value)}/></label><label className="wide">说明<textarea value={description} onChange={event => setDescription(event.target.value)}/></label></section><footer><button type="button" className="ghost" onClick={() => setCreating(false)}>取消</button><button className="primary" disabled={create.isPending}>{create.isPending ? '创建中…' : '创建环境'}</button></footer></form></div>}
    {terminal?.state === 'PUBLISHING' ? <PublishingPanel onClose={closeTerminal}/> : terminal && (
      <TerminalPanel
        session={terminal}
        publishError={terminalError}
        onClose={closeTerminal}
        onPublishing={() => { setTerminal(current => current ? { ...current, state: 'PUBLISHING', error_detail: null } : current); setTerminalError(''); }}
        onPublishFailed={message => { setTerminal(current => current ? { ...current, state: 'RUNNING' } : current); setTerminalError(message); void queryClient.invalidateQueries({ queryKey: ['terminal-environments'] }); }}
      />
    )}
  </main>;
}
