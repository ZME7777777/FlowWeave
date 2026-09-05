import { Activity, Bot, Boxes, BrainCircuit, CalendarClock, ChevronDown, GitFork, Hexagon, KeyRound, Library, LogOut, PlayCircle, Settings, TerminalSquare } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { FlowsPage } from './pages/FlowsPage';
import { NodesPage } from './pages/NodesPage';
import { ModelsPage } from './pages/ModelsPage';
import { RunsPage } from './pages/RunsPage';
import { SchedulesPage } from './pages/SchedulesPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { CapabilitiesPage } from './pages/CapabilitiesPage';
import { TerminalEnvironmentsPage } from './pages/TerminalEnvironmentsPage';
import { CredentialsPage } from './pages/CredentialsPage';
import { StandaloneAgentTerminal } from './components/AgentRuntimeSidebar';
import { AgentWorkbenchPage } from './pages/AgentWorkbenchPage';
import { FlowNodeSessionPage } from './pages/FlowNodeSessionPage';
import { useWorkbenchStore } from './store/workbench';
import { withDeploymentBase, withoutDeploymentBase } from './deploymentPath';
import { api } from './api/client';
import type { AuthUser } from './types';
import { LoginScreen } from './components/LoginScreen';
import { useEscapeClose } from './components/useEscapeClose';

const nav = [
  { view: 'nodes' as const, label: '节点资产', icon: Boxes },
  { view: 'capabilities' as const, label: '能力仓库', icon: Library },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: PlayCircle },
  { view: 'schedules' as const, label: '定时任务', icon: CalendarClock },
  { view: 'agent-workbench' as const, label: 'Agent 会话', icon: Bot },
];

const settingsNav = [
  { view: 'environments' as const, label: '终端环境', description: '管理 Agent 运行环境', icon: TerminalSquare },
  { view: 'credentials' as const, label: '认证管理', description: '维护访问凭据与密钥', icon: KeyRound },
  { view: 'models' as const, label: '大模型配置', description: '配置模型服务与可用模型', icon: BrainCircuit },
];

export function App() {
  const { view, setView } = useWorkbenchStore();
  const [, setRouteVersion] = useState(0);
  const [user, setUser] = useState<AuthUser | null>();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsMenu = useRef<HTMLDivElement>(null);
  useEscapeClose(() => setSettingsOpen(false), settingsOpen);
  useEffect(() => {
    if (!settingsOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!settingsMenu.current?.contains(event.target as Node)) setSettingsOpen(false);
    };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [settingsOpen]);
  useEffect(() => {
    let active = true;
    void api.authMe().then(value => { if (active) setUser(value); }).catch(() => {
      if (active) setUser(null);
    });
    const requireLogin = () => setUser(null);
    window.addEventListener('flowweave:auth-required', requireLogin);
    return () => {
      active = false;
      window.removeEventListener('flowweave:auth-required', requireLogin);
    };
  }, []);
  useEffect(() => {
    const update = (event: PopStateEvent) => {
      const flowRun = event.state?.flowweaveFlowRun;
      if (
        flowRun
        && typeof flowRun.runId === 'string'
        && typeof flowRun.nodeRunId === 'string'
        && typeof flowRun.attemptId === 'string'
      ) {
        // Node sessions are a routed leaf of the selected Attempt. Restore
        // that in-memory selection when Back returns to the originating
        // workbench entry instead of falling through to the run list.
        useWorkbenchStore.setState({
          view: 'workbench',
          selectedRunId: flowRun.runId,
          selectedNodeRunId: flowRun.nodeRunId,
          selectedAttemptId: flowRun.attemptId,
          selectedWorkbenchMode: flowRun.mode === 'AUTOMATIC' ? 'AUTOMATIC' : 'MANUAL',
          selectedAutomaticRecordId: flowRun.mode === 'AUTOMATIC' && typeof flowRun.automaticRecordId === 'string'
            ? flowRun.automaticRecordId
            : undefined,
        });
      }
      setRouteVersion(value => value + 1);
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  const navigate = (path: string, replace = false) => {
    const deployedPath = withDeploymentBase(path);
    // Conversations may push their own binding URL. Keep the source Workbench
    // selection on those entries so an explicit return works after switching
    // between conversations, not only from the initial session route.
    const source = nodeSessionRoute ? window.history.state?.flowweaveFlowRun : undefined;
    const state = source ? { flowweaveFlowRun: source } : {};
    if (replace) window.history.replaceState(state, '', deployedPath);
    else window.history.pushState(state, '', deployedPath);
    setRouteVersion(value => value + 1);
  };
  const routePathname = withoutDeploymentBase(window.location.pathname);
  const nodeSessionRoute = routePathname.match(
    /^\/flow-runs\/([^/]+)\/nodes\/([^/]+)\/attempts\/([^/]+)\/agent-sessions(?:\/([^/]+))?$/,
  );
  const isAgentRoute = routePathname === '/agent' || routePathname.startsWith('/agent/conversations/') || Boolean(nodeSessionRoute);
  const leaveAgentRoute = () => {
    if (isAgentRoute) navigate('/', true);
  };
  const selectView = (next: typeof view) => {
    setSettingsOpen(false);
    if (next === 'agent-workbench') {
      navigate('/agent');
      setView(next);
      return;
    }
    leaveAgentRoute();
    setView(next);
  };
  const terminalParams = new URLSearchParams(window.location.search);
  const terminalRunId = terminalParams.get('terminalRun');
  const terminalConversationId = terminalParams.get('terminalConversation');
  if (user === undefined) return <div className="auth-loading"><Hexagon size={30} fill="currentColor"/><span>正在验证登录状态…</span></div>;
  if (user === null) return <LoginScreen onLogin={setUser}/>;
  const logout = async () => {
    try { await api.logout(); } finally { setUser(null); }
  };
  if (terminalRunId && terminalConversationId) return <StandaloneAgentTerminal runId={terminalRunId} conversationId={terminalConversationId}/>;
  const renderedView = view === 'agent-workbench' ? 'nodes' : view;
  const activeSettingsItem = !isAgentRoute ? settingsNav.find(item => item.view === renderedView) : undefined;
  const settingsActive = Boolean(activeSettingsItem);
  return <div className={`app-shell${isAgentRoute ? ' agent-workbench-shell' : ''}`}><header className="topbar"><button className="brand" onClick={() => selectView('nodes')} aria-label="返回节点资产"><span className="brand-mark"><Hexagon size={22} fill="currentColor"/></span><span>FlowWeave</span></button><nav aria-label="主导航">{nav.map(item => <button key={item.view} className={(nodeSessionRoute ? item.view === 'runs' : isAgentRoute ? item.view === 'agent-workbench' : renderedView === item.view) ? 'active' : ''} onClick={() => selectView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav>{activeSettingsItem && <div className="context-tab" aria-label={`当前配置：${activeSettingsItem.label}`}><span className="context-tab-divider" aria-hidden="true"/><activeSettingsItem.icon size={14}/><span>{activeSettingsItem.label}</span></div>}<div className="topbar-actions"><span className="kernel-status"><Activity size={14}/>{nodeSessionRoute ? 'FlowRun 节点会话' : isAgentRoute ? 'Agent 工作区' : '产物驱动运行'}</span><div className="account-menu" ref={settingsMenu}><button type="button" className={`account-trigger${settingsActive ? ' active' : ''}`} aria-label="账户与设置" aria-haspopup="menu" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(open => !open)}><span className="account-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span><span className="account-identity"><b>{user.username}</b><small>{user.is_super_admin ? '超级管理员' : '普通用户'}</small></span><ChevronDown size={13} className={settingsOpen ? 'rotated' : ''}/></button>{settingsOpen && <div className="settings-popover account-popover" role="menu" aria-label="账户与设置"><div className="account-popover-heading"><Settings size={14}/><span><b>平台设置</b><small>管理运行环境与平台服务</small></span></div>{settingsNav.map(item => <button type="button" role="menuitem" key={item.view} className={renderedView === item.view && !isAgentRoute ? 'active' : ''} onClick={() => selectView(item.view)}><span className="settings-item-icon"><item.icon size={16}/></span><span><b>{item.label}</b><small>{item.description}</small></span></button>)}<button type="button" role="menuitem" className="account-logout" onClick={() => void logout()}><span className="settings-item-icon"><LogOut size={16}/></span><span><b>退出登录</b><small>结束当前平台会话</small></span></button></div>}</div></div></header>
    {isAgentRoute ? nodeSessionRoute ? <FlowNodeSessionPage flowRunId={decodeURIComponent(nodeSessionRoute[1])} nodeRunId={decodeURIComponent(nodeSessionRoute[2])} attemptId={decodeURIComponent(nodeSessionRoute[3])} onNavigate={navigate}/> : <AgentWorkbenchPage onNavigate={navigate}/> : <>{<div className="principle-bar">一个 FlowRun 共享一个可替换 Runtime 与 Workspace；全部会话保留各自的 OpenHands 原生身份和事件树。</div>}{renderedView === 'nodes' && <NodesPage/>}{renderedView === 'capabilities' && <CapabilitiesPage/>}{renderedView === 'environments' && <TerminalEnvironmentsPage/>}{renderedView === 'credentials' && <CredentialsPage/>}{renderedView === 'flows' && <FlowsPage/>}{renderedView === 'runs' && <RunsPage/>}{renderedView === 'schedules' && <SchedulesPage/>}{renderedView === 'models' && <ModelsPage/>}{renderedView === 'workbench' && <WorkbenchPage/>}</>}
  </div>;
}
