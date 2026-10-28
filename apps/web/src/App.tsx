import { ViewportPopover } from './components/ViewportPopover';
import { AudioLines, Box, BrainCircuit, CalendarClock, ChevronDown, Component, GitFork, Hexagon, KeyRound, Library, LogOut, MessagesSquare, PlayCircle, Settings, TerminalSquare, UsersRound, X } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useWorkbenchStore } from './store/workbench';
import { clearContextTabs, readContextTabs, saveContextTabs, type ContextTab, type FlowRunTab, type SettingsView } from './store/context-tabs';
import { withDeploymentBase, withoutDeploymentBase } from './deploymentPath';
import { api } from './api/client';
import { flowRunLocationFromState, flowRunPath, flowRunTabId, flowRunTabLabel, parseFlowRunPath, type FlowRunLocation } from './flowRunNavigation';
import type { AuthUser, FlowRunSummary } from './types';
import { LoginScreen } from './components/LoginScreen';
import { workspaceFileViewerRoute } from './pages/workspace-file-viewer-route';
import { useEscapeClose } from './components/useEscapeClose';
import { clearAgentSessionCacheStorage, setAgentSessionCacheIdentity } from './components/agent-session/conversation-cache';

const FlowsPage = lazy(() => import('./pages/FlowsPage').then(module => ({ default: module.FlowsPage })));
const NodesPage = lazy(() => import('./pages/NodesPage').then(module => ({ default: module.NodesPage })));
const ModelsPage = lazy(() => import('./pages/ModelsPage').then(module => ({ default: module.ModelsPage })));
const RunsPage = lazy(() => import('./pages/RunsPage').then(module => ({ default: module.RunsPage })));
const CasesPage = lazy(() => import('./pages/CasesPage').then(module => ({ default: module.CasesPage })));
const SchedulesPage = lazy(() => import('./pages/SchedulesPage').then(module => ({ default: module.SchedulesPage })));
const WorkbenchPage = lazy(() => import('./pages/WorkbenchPage').then(module => ({ default: module.WorkbenchPage })));
const CapabilitiesPage = lazy(() => import('./pages/CapabilitiesPage').then(module => ({ default: module.CapabilitiesPage })));
const TerminalEnvironmentsPage = lazy(() => import('./pages/TerminalEnvironmentsPage').then(module => ({ default: module.TerminalEnvironmentsPage })));
const CredentialsPage = lazy(() => import('./pages/CredentialsPage').then(module => ({ default: module.CredentialsPage })));
const LdapUsersPage = lazy(() => import('./pages/LdapUsersPage').then(module => ({ default: module.LdapUsersPage })));
const StandaloneAgentTerminal = lazy(() => import('./components/AgentRuntimeSidebar').then(module => ({ default: module.StandaloneAgentTerminal })));
const AgentWorkbenchPage = lazy(() => import('./pages/AgentWorkbenchPage').then(module => ({ default: module.AgentWorkbenchPage })));
const FlowNodeSessionPage = lazy(() => import('./pages/FlowNodeSessionPage').then(module => ({ default: module.FlowNodeSessionPage })));
const CaseSessionPage = lazy(() => import('./pages/CaseSessionPage').then(module => ({ default: module.CaseSessionPage })));
const WorkspaceFileViewerPage = lazy(() => import('./pages/WorkspaceFileViewerPage').then(module => ({ default: module.WorkspaceFileViewerPage })));

function PageLoading() {
  return <main className="auth-loading" role="status"><Hexagon size={30} fill="currentColor"/><span>正在加载页面…</span></main>;
}

const nav = [
  { view: 'nodes' as const, label: '节点中心', icon: Component },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: AudioLines },
  { view: 'schedules' as const, label: '定时调度', icon: CalendarClock },
  { view: 'cases' as const, label: '场景空间', icon: Box },
  { view: 'capabilities' as const, label: '能力仓库', icon: Library },
  { view: 'agent-workbench' as const, label: 'AI 工作台', icon: MessagesSquare },
];

const settingsNav = [
  { view: 'environments' as const, label: '终端环境', description: '管理 Agent 运行环境', icon: TerminalSquare },
  { view: 'credentials' as const, label: '认证管理', description: '维护访问凭据与密钥', icon: KeyRound },
  { view: 'models' as const, label: '大模型配置', description: '配置模型服务与可用模型', icon: BrainCircuit },
];

const superAdminSettingsNav = [
  { view: 'ldap-users' as const, label: '用户管理', description: '管理 LDAP 授权和平台账户资料', icon: UsersRound },
];

const LAST_STANDALONE_AGENT_PATH_KEY = 'flowweave.agent.last-standalone-path.v1';

function isStandaloneAgentPath(path: string): boolean {
  try {
    const url = new URL(path, window.location.origin);
    return /^\/agent(?:\/schedules)?$/.test(url.pathname)
      || /^\/agent\/(?:conversations|schedules\/conversations)\/[^/]+$/.test(url.pathname);
  } catch {
    return false;
  }
}

function readLastStandaloneAgentPath(): string {
  try {
    const path = sessionStorage.getItem(LAST_STANDALONE_AGENT_PATH_KEY);
    return path && isStandaloneAgentPath(path) ? path : '/agent';
  } catch {
    return '/agent';
  }
}

function clearAgentIdentityStorage() {
  const isAgentStorage = (key: string) => key.startsWith('flowweave.agent.')
    || key.startsWith('flowweave:agent-session-tools:agent-workspace:')
    || key.startsWith('flowweave:agent-conversation-');
  for (const key of Object.keys(sessionStorage)) {
    if (isAgentStorage(key)) sessionStorage.removeItem(key);
  }
  for (const key of Object.keys(localStorage)) {
    if (isAgentStorage(key)) localStorage.removeItem(key);
  }
  clearAgentSessionCacheStorage();
}

export function App() {
  const queryClient = useQueryClient();
  const { view, selectedRunId, setView } = useWorkbenchStore();
  const nodeLeaveGuard = useRef<(() => Promise<boolean>) | undefined>(undefined);
  const registerNodeLeaveGuard = useCallback((guard: (() => Promise<boolean>) | undefined) => { nodeLeaveGuard.current = guard; }, []);
  const [contentReloadVersion, setContentReloadVersion] = useState(0);
  const [, setRouteVersion] = useState(0);
  const [user, setUser] = useState<AuthUser | null>();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [openSettingsViews, setOpenSettingsViews] = useState<SettingsView[]>([]);
  const [openFlowRunTabs, setOpenFlowRunTabs] = useState<FlowRunTab[]>([]);
  const [openContextTabs, setOpenContextTabs] = useState<ContextTab[]>([]);
  const tabsUser = useRef<AuthUser | null>(null);
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('view') === 'cases') setView('cases');
  }, [setView]);
  const restoreContextTabs = useCallback((nextUser: AuthUser) => {
    const saved = readContextTabs(nextUser);
    tabsUser.current = nextUser;
    setOpenSettingsViews(saved.settings);
    setOpenFlowRunTabs(saved.runs);
    setOpenContextTabs(saved.order);
  }, []);
  useEffect(() => {
    if (user && tabsUser.current === user) saveContextTabs(user.id, openContextTabs, openFlowRunTabs);
  }, [openContextTabs, openFlowRunTabs, user]);
  const lastStandaloneAgentPath = useRef(readLastStandaloneAgentPath());
  const settingsMenu = useRef<HTMLDivElement>(null);
  const settingsPopover = useRef<HTMLDivElement>(null);
  const setLastStandaloneAgentPath = useCallback((path: string) => {
    lastStandaloneAgentPath.current = path;
    try { sessionStorage.setItem(LAST_STANDALONE_AGENT_PATH_KEY, path); } catch { /* browser session state only */ }
  }, []);
  const rememberStandaloneAgentConversation = useCallback((path: string) => {
    try {
      const url = new URL(path, window.location.origin);
      if (/^\/agent\/(?:conversations|schedules\/conversations)\/[^/]+$/.test(url.pathname)
        || (url.pathname === '/agent' && url.searchParams.has('draft_work_directory_id'))) {
        setLastStandaloneAgentPath(`${url.pathname}${url.search}${url.hash}`);
      }
    } catch {
      // Invalid browser history values must not replace the last valid target.
    }
  }, [setLastStandaloneAgentPath]);
  const restoreStandaloneAgentPath = useCallback((path: string) => {
    if (isStandaloneAgentPath(path)) setLastStandaloneAgentPath(path);
  }, [setLastStandaloneAgentPath]);
  const selectStandaloneAgentRootDraft = useCallback(() => {
    setLastStandaloneAgentPath('/agent');
  }, [setLastStandaloneAgentPath]);
  const replaceIdentity = useCallback(async (nextUser: AuthUser | null) => {
    // Unmount authenticated product surfaces before an old request can
    // finish under the newly authenticated browser session. The default
    // Agent Workspace ID is shared, so retaining its query key would reuse
    // the previous account's conversation projection for up to staleTime.
    setUser(undefined);
    setSettingsOpen(false);
    if (tabsUser.current) clearContextTabs(tabsUser.current.id);
    tabsUser.current = null;
    setOpenSettingsViews([]);
    setOpenFlowRunTabs([]);
    setOpenContextTabs([]);
    if ([...settingsNav, ...superAdminSettingsNav].some(item => item.view === useWorkbenchStore.getState().view)) setView('nodes');
    await queryClient.cancelQueries();
    queryClient.clear();
    clearAgentIdentityStorage();
    const pathname = withoutDeploymentBase(window.location.pathname);
    if (pathname.startsWith('/agent/conversations/') || pathname.startsWith('/agent/schedules/conversations/')) {
      window.history.replaceState({}, '', withDeploymentBase('/agent'));
      setView('agent-workbench');
      setRouteVersion(value => value + 1);
    }
    if (nextUser) {
      setAgentSessionCacheIdentity(nextUser.id);
      restoreContextTabs(nextUser);
    }
    setUser(nextUser);
  }, [queryClient, restoreContextTabs, setView]);
  useEscapeClose(() => setSettingsOpen(false), settingsOpen);
  const reloadCurrentContent = useCallback(async () => {
    if (nodeLeaveGuard.current && !await nodeLeaveGuard.current()) return;
    setSettingsOpen(false);
    await queryClient.cancelQueries();
    queryClient.clear();
    setContentReloadVersion(current => current + 1);
  }, [queryClient]);
  useEffect(() => {
    if (!settingsOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!settingsMenu.current?.contains(event.target as Node) && !settingsPopover.current?.contains(event.target as Node)) setSettingsOpen(false);
    };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [settingsOpen]);
  useEffect(() => {
    let active = true;
    void api.authMe().then(value => {
      if (!active) return;
      setAgentSessionCacheIdentity(value.id);
      restoreContextTabs(value);
      setUser(value);
    }).catch(() => {
      if (active) void replaceIdentity(null);
    });
    const requireLogin = () => { void replaceIdentity(null); };
    window.addEventListener('flowweave:auth-required', requireLogin);
    return () => {
      active = false;
      window.removeEventListener('flowweave:auth-required', requireLogin);
    };
  }, [replaceIdentity, restoreContextTabs]);
  const toggleFullscreenFromTopbar = (event: ReactMouseEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest('button, a, input, select, textarea, [contenteditable="true"]')) return;
    if (!document.fullscreenEnabled) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void document.documentElement.requestFullscreen();
    }
  };
  const routePathname = withoutDeploymentBase(window.location.pathname);
  const fileViewerRoute = workspaceFileViewerRoute();
  const flowRunRoute = parseFlowRunPath(routePathname);
  const nodeSessionRoute = routePathname.match(
    /^\/flow-runs\/([^/]+)\/nodes\/([^/]+)\/attempts\/([^/]+)\/agent-sessions(?:\/([^/]+))?$/,
  );
  const caseSessionRoute = routePathname.match(/^\/cases\/([^/]+)\/conversations\/([^/]+)$/);
  const flowRunRouteId = flowRunRoute?.runId;
  const nodeSessionLocation = nodeSessionRoute
    ? flowRunLocationFromState(window.history.state) ?? { runId: decodeURIComponent(nodeSessionRoute[1]) }
    : undefined;
  const savedFlowRunLocation = flowRunLocationFromState(window.history.state);
  const activeFlowRunLocation = flowRunRoute
    ? !flowRunRoute.recordId && savedFlowRunLocation?.runId === flowRunRoute.runId ? savedFlowRunLocation : flowRunRoute
    : nodeSessionLocation;
  const activeFlowRunTabId = activeFlowRunLocation ? flowRunTabId(activeFlowRunLocation) : undefined;
  const registerFlowRunTab = useCallback((run: Pick<FlowRunSummary, 'id' | 'run_no'>, location: FlowRunLocation) => {
    const id = flowRunTabId(location);
    const label = flowRunTabLabel(run.run_no, location);
    setOpenFlowRunTabs(current => {
      const existing = current.find(item => item.id === id);
      return !existing || existing.label === label ? current : current.map(item => item.id === id ? { ...item, label } : item);
    });
  }, []);
  useEffect(() => {
    const update = (event: PopStateEvent) => {
      const flowRun = event.state?.flowweaveFlowRun;
      if (flowRun && typeof flowRun.runId === 'string') {
        useWorkbenchStore.setState({
          view: 'workbench',
          selectedRunId: flowRun.runId,
          selectedNodeRunId: typeof flowRun.nodeRunId === 'string' ? flowRun.nodeRunId : undefined,
          selectedAttemptId: typeof flowRun.attemptId === 'string' ? flowRun.attemptId : undefined,
          selectedWorkbenchMode: flowRun.mode === 'AUTOMATIC' ? 'AUTOMATIC' : 'MANUAL',
          selectedAutomaticRecordId: flowRun.mode === 'AUTOMATIC' && typeof flowRun.automaticRecordId === 'string'
            ? flowRun.automaticRecordId
            : undefined,
          selectedStepwiseRecordId: flowRun.mode === 'MANUAL' && typeof flowRun.stepwiseRecordId === 'string'
            ? flowRun.stepwiseRecordId
            : undefined,
        });
      } else {
        const path = withoutDeploymentBase(window.location.pathname);
        if (path === '/') {
          useWorkbenchStore.setState({
            view: 'runs', selectedRunId: undefined, selectedNodeRunId: undefined, selectedAttemptId: undefined,
            selectedWorkbenchMode: undefined, selectedAutomaticRecordId: undefined, selectedStepwiseRecordId: undefined,
          });
        }
        restoreStandaloneAgentPath(`${path}${window.location.search}${window.location.hash}`);
      }
      setRouteVersion(value => value + 1);
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, [restoreStandaloneAgentPath]);
  const navigate = (path: string, replace = false, options?: { standaloneAgentReturnTarget?: 'root' }) => {
    if (options?.standaloneAgentReturnTarget === 'root' && path === '/agent') selectStandaloneAgentRootDraft();
    else rememberStandaloneAgentConversation(path);
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
  useLayoutEffect(() => {
    if (!user || !activeFlowRunTabId) return;
    const route = parseFlowRunPath(routePathname);
    const saved = flowRunLocationFromState(window.history.state);
    const location = route ? !route.recordId && saved?.runId === route.runId ? saved : route : saved ?? { runId: activeFlowRunTabId };
    if (flowRunRouteId) {
      const flowRunRoute = location;
      const source = window.history.state?.flowweaveFlowRun;
      const savedLocation = flowRunLocationFromState(window.history.state);
      const matchingHistory = savedLocation && flowRunTabId(savedLocation) === activeFlowRunTabId;
      // The URL is authoritative, including when another record of this same
      // parent is already mounted or history state belongs to an older record.
      useWorkbenchStore.setState({
        view: 'workbench', selectedRunId: flowRunRoute.runId,
        selectedNodeRunId: matchingHistory && typeof source?.nodeRunId === 'string' ? source.nodeRunId : undefined,
        selectedAttemptId: matchingHistory && typeof source?.attemptId === 'string' ? source.attemptId : undefined,
        selectedWorkbenchMode: flowRunRoute.mode,
        selectedAutomaticRecordId: flowRunRoute.mode === 'AUTOMATIC' ? flowRunRoute.recordId : undefined,
        selectedStepwiseRecordId: flowRunRoute.mode === 'MANUAL' ? flowRunRoute.recordId : undefined,
      });
    }
    const path = `${routePathname}${window.location.search}${window.location.hash}`;
    const state = window.history.state ?? {};
    setOpenContextTabs(tabs => tabs.some(tab => tab.kind === 'flow-run' && tab.id === activeFlowRunTabId)
      ? tabs
      : [...tabs, { kind: 'flow-run', id: activeFlowRunTabId }]);
    setOpenFlowRunTabs(current => {
      const existing = current.find(tab => tab.id === activeFlowRunTabId);
      if (!existing) return [...current, { id: activeFlowRunTabId, label: '正在打开运行…', path, state }];
      return existing.path === path && existing.state === state
        ? current
        : current.map(tab => tab.id === activeFlowRunTabId ? { ...tab, path, state } : tab);
    });
    void api.flowRun(location.runId).then(run => {
      if (tabsUser.current === user) registerFlowRunTab(run, location);
    }).catch(() => undefined);
  }, [activeFlowRunTabId, flowRunRouteId, registerFlowRunTab, routePathname, user]);
  useLayoutEffect(() => {
    if (flowRunRouteId || nodeSessionRoute || view !== 'workbench' || !selectedRunId) return;
    const selection = useWorkbenchStore.getState();
    const state = {
      flowweaveFlowRun: {
        runId: selectedRunId,
        nodeRunId: selection.selectedNodeRunId,
        attemptId: selection.selectedAttemptId,
        mode: selection.selectedWorkbenchMode,
        automaticRecordId: selection.selectedAutomaticRecordId,
        stepwiseRecordId: selection.selectedStepwiseRecordId,
      },
    };
    const recordId = selection.selectedWorkbenchMode === 'AUTOMATIC' ? selection.selectedAutomaticRecordId : selection.selectedStepwiseRecordId;
    window.history.pushState(state, '', withDeploymentBase(flowRunPath({ runId: selectedRunId, mode: selection.selectedWorkbenchMode, recordId })));
    setRouteVersion(value => value + 1);
  }, [flowRunRouteId, nodeSessionRoute, selectedRunId, view]);
  const directAgentRoute = routePathname === '/agent' || routePathname === '/agent/schedules' || routePathname.startsWith('/agent/conversations/') || routePathname.startsWith('/agent/schedules/conversations/');
  const canUseAgentSessions = user?.can_use_agent_sessions !== false;
  const isAgentRoute = (directAgentRoute && canUseAgentSessions) || Boolean(nodeSessionRoute) || Boolean(caseSessionRoute);
  useLayoutEffect(() => {
    if (directAgentRoute) rememberStandaloneAgentConversation(routePathname);
  }, [directAgentRoute, rememberStandaloneAgentConversation, routePathname]);
  useEffect(() => {
    if (!directAgentRoute || user?.can_use_agent_sessions !== false) return;
    window.history.replaceState({}, '', withDeploymentBase('/'));
    setView('nodes');
    setRouteVersion(value => value + 1);
  }, [directAgentRoute, setView, user?.can_use_agent_sessions]);
  const leaveAgentRoute = () => {
    if (!isAgentRoute) return;
    if (caseSessionRoute) {
      setView('cases');
      navigate('/', true);
      return;
    }
    navigate('/', true);
  };
  const leaveFlowRunRoute = () => {
    if (flowRunRouteId) navigate('/', true);
  };
  useLayoutEffect(() => {
    if (!user || isAgentRoute || flowRunRouteId) return;
    const availableSettings = user.is_super_admin
      ? [...settingsNav, ...superAdminSettingsNav]
      : settingsNav;
    if (view === 'ldap-users' && !user.is_super_admin) {
      setView('nodes');
      return;
    }
    if (!availableSettings.some(item => item.view === view)) return;
    const settingsView = view as SettingsView;
    setOpenSettingsViews(current => current.includes(settingsView) ? current : [...current, settingsView]);
    setOpenContextTabs(tabs => tabs.some(tab => tab.kind === 'settings' && tab.view === settingsView)
      ? tabs
      : [...tabs, { kind: 'settings', view: settingsView }]);
  }, [flowRunRouteId, isAgentRoute, setView, user, view]);
  const selectView = async (next: typeof view) => {
    if (next !== 'nodes' && nodeLeaveGuard.current && !await nodeLeaveGuard.current()) return false;
    setSettingsOpen(false);
    if (next === 'agent-workbench') {
      if (user?.can_use_agent_sessions === false) {
        leaveAgentRoute();
        leaveFlowRunRoute();
        setView('nodes');
        return true;
      }
      navigate(lastStandaloneAgentPath.current);
      setView(next);
      return true;
    }
    leaveAgentRoute();
    leaveFlowRunRoute();
    setView(next);
    return true;
  };
  const selectSettingsView = async (next: SettingsView) => {
    if (!await selectView(next)) return;
    setOpenSettingsViews(current => current.includes(next) ? current : [...current, next]);
    setOpenContextTabs(tabs => tabs.some(tab => tab.kind === 'settings' && tab.view === next)
      ? tabs
      : [...tabs, { kind: 'settings', view: next }]);
  };
  const closeSettingsView = (closingView: SettingsView) => {
    const closingIndex = openSettingsViews.indexOf(closingView);
    const next = openSettingsViews.filter(item => item !== closingView);
    setOpenSettingsViews(next);
    setOpenContextTabs(tabs => tabs.filter(tab => tab.kind !== 'settings' || tab.view !== closingView));
    if (renderedView !== closingView) return;
    const replacement = next[closingIndex] ?? next[closingIndex - 1];
    if (replacement) selectView(replacement);
    else selectView('nodes');
  };
  const selectFlowRunTab = async (runId: string) => {
    if (nodeLeaveGuard.current && !await nodeLeaveGuard.current()) return;
    const tab = openFlowRunTabs.find(item => item.id === runId);
    if (!tab) {
      navigate(`/flow-runs/${encodeURIComponent(runId)}`);
      return;
    }
    window.history.pushState(tab.state, '', withDeploymentBase(tab.path));
    // Restore the saved Attempt/record selection through the same path as Back.
    // After a refresh it is no longer present in the in-memory workbench store.
    window.dispatchEvent(new PopStateEvent('popstate', { state: tab.state }));
  };
  const closeFlowRunTab = (runId: string) => {
    const closingIndex = openFlowRunTabs.findIndex(tab => tab.id === runId);
    const next = openFlowRunTabs.filter(tab => tab.id !== runId);
    setOpenFlowRunTabs(next);
    setOpenContextTabs(tabs => tabs.filter(tab => tab.kind !== 'flow-run' || tab.id !== runId));
    if (activeFlowRunTabId !== runId) return;
    const replacement = next[closingIndex] ?? next[closingIndex - 1];
    if (replacement) selectFlowRunTab(replacement.id);
    else {
      useWorkbenchStore.setState({ view: 'runs', selectedRunId: undefined, selectedNodeRunId: undefined, selectedAttemptId: undefined });
      navigate('/', true);
    }
  };
  const terminalParams = new URLSearchParams(window.location.search);
  const terminalRunId = terminalParams.get('terminalRun');
  const terminalRunName = terminalParams.get('terminalName');
  const terminalConversationId = terminalParams.get('terminalConversation');
  if (user === undefined) return <div className="auth-loading"><Hexagon size={30} fill="currentColor"/><span>正在验证登录状态…</span></div>;
  if (user === null) return <LoginScreen onLogin={replaceIdentity}/>;
  if (fileViewerRoute) return <Suspense fallback={<PageLoading/>}><WorkspaceFileViewerPage route={fileViewerRoute}/></Suspense>;
  const logout = async () => {
    if (nodeLeaveGuard.current && !await nodeLeaveGuard.current()) return;
    try { await api.logout(); } finally { await replaceIdentity(null); }
  };
  if (terminalRunId) return <Suspense fallback={<PageLoading/>}><StandaloneAgentTerminal runId={terminalRunId} runName={terminalRunName} conversationId={terminalConversationId}/></Suspense>;
  const renderedView = view === 'agent-workbench' ? 'nodes' : view;
  const accountSettingsNav = user.is_super_admin
    ? [...settingsNav, ...superAdminSettingsNav]
    : settingsNav;
  const activeSettingsItem = !isAgentRoute ? accountSettingsNav.find(item => item.view === renderedView) : undefined;
  const settingsActive = Boolean(activeSettingsItem);
  const visibleNav = nav.filter(item => item.view !== 'agent-workbench' || user.can_use_agent_sessions !== false);
  const hasContextTabs = openContextTabs.length > 0;
  return <div className={`app-shell${isAgentRoute ? ' agent-workbench-shell' : ''}`}><header className="topbar" onDoubleClick={toggleFullscreenFromTopbar}><button type="button" className="brand" aria-label="重新加载当前页面数据" title="重新加载当前页面数据（保持全屏）" onClick={() => void reloadCurrentContent()}><span className="brand-mark"><Hexagon size={22} fill="currentColor"/></span><span>FlowWeave</span></button><nav aria-label="主导航">{visibleNav.map(item => <button key={item.view} className={(!activeFlowRunTabId && (isAgentRoute ? item.view === 'agent-workbench' : renderedView === item.view)) ? 'active' : ''} onClick={() => selectView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav>{hasContextTabs && <div className="context-tabs" aria-label="临时管理标签">{openContextTabs.map(contextTab => {
    if (contextTab.kind === 'settings') {
      const item = accountSettingsNav.find(candidate => candidate.view === contextTab.view);
      if (!item) return null;
      return <div className={`context-tab${renderedView === item.view && !isAgentRoute && !flowRunRouteId ? ' active' : ''}`} key={`settings-${item.view}`}><button type="button" className="context-tab-select" aria-current={renderedView === item.view && !isAgentRoute && !flowRunRouteId ? 'page' : undefined} title={item.label} onClick={() => selectSettingsView(item.view)}><item.icon size={14}/><span>{item.label}</span></button><button type="button" className="context-tab-close" aria-label={`关闭${item.label}`} title={`关闭${item.label}`} onClick={() => closeSettingsView(item.view)}><X size={12}/></button></div>;
    }
    const tab = openFlowRunTabs.find(candidate => candidate.id === contextTab.id);
    if (!tab) return null;
    return <div className={`context-tab${activeFlowRunTabId === tab.id ? ' active' : ''}`} key={`flow-run-${tab.id}`}><button type="button" className="context-tab-select" aria-current={activeFlowRunTabId === tab.id ? 'page' : undefined} title={`${tab.label} · ${tab.path}`} onClick={() => selectFlowRunTab(tab.id)}><PlayCircle size={14}/><span>{tab.label}</span></button><button type="button" className="context-tab-close" aria-label={`关闭${tab.label}`} title={`关闭${tab.label}`} onClick={() => closeFlowRunTab(tab.id)}><X size={12}/></button></div>;
  })}</div>}<div className="topbar-actions"><div className="account-menu" ref={settingsMenu}><button type="button" className={`account-trigger${settingsActive ? ' active' : ''}`} aria-label="账户与设置" aria-haspopup="menu" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(open => !open)}><span className="account-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span><span className="account-identity"><b>{user.username}</b><small>{user.is_super_admin ? '超级管理员' : '普通用户'}</small></span><ChevronDown size={13} className={settingsOpen ? 'rotated' : ''}/></button>{settingsOpen && <ViewportPopover ref={settingsPopover} anchor={() => settingsMenu.current} align="end" matchAnchorWidth className="settings-popover account-popover" role="menu" aria-label="账户与设置"><div className="account-popover-heading"><Settings size={14}/><b>平台设置</b></div>{accountSettingsNav.map(item => <button type="button" role="menuitem" title={item.description} key={item.view} className={renderedView === item.view && !isAgentRoute && !flowRunRouteId ? 'active' : ''} onClick={() => selectSettingsView(item.view)}><span className="settings-item-icon"><item.icon size={15}/></span><b>{item.label}</b></button>)}<button type="button" role="menuitem" title="结束当前平台会话" className="account-logout" onClick={() => void logout()}><span className="settings-item-icon"><LogOut size={15}/></span><b>退出登录</b></button></ViewportPopover>}</div></div></header>
    <Suspense fallback={<PageLoading/>}><div key={contentReloadVersion}>{isAgentRoute ? nodeSessionRoute ? <FlowNodeSessionPage flowRunId={decodeURIComponent(nodeSessionRoute[1])} nodeRunId={decodeURIComponent(nodeSessionRoute[2])} attemptId={decodeURIComponent(nodeSessionRoute[3])} onNavigate={navigate}/> : caseSessionRoute ? <CaseSessionPage caseId={decodeURIComponent(caseSessionRoute[1])} bindingId={decodeURIComponent(caseSessionRoute[2])} onNavigate={navigate} onReturn={() => { setView('cases'); navigate('/', true); }}/> : <AgentWorkbenchPage onNavigate={navigate}/> : <>{renderedView === 'nodes' && <NodesPage onRegisterLeaveGuard={registerNodeLeaveGuard}/>}{renderedView === 'capabilities' && <CapabilitiesPage/>}{renderedView === 'environments' && <TerminalEnvironmentsPage/>}{renderedView === 'credentials' && <CredentialsPage/>}{renderedView === 'ldap-users' && user.is_super_admin && <LdapUsersPage/>}{renderedView === 'flows' && <FlowsPage/>}{renderedView === 'runs' && <RunsPage/>}{renderedView === 'cases' && <CasesPage onNavigate={navigate}/>}{renderedView === 'schedules' && <SchedulesPage/>}{renderedView === 'models' && <ModelsPage/>}{renderedView === 'workbench' && <WorkbenchPage key={activeFlowRunTabId ?? selectedRunId} location={activeFlowRunLocation}/>}</>}</div></Suspense>
  </div>;
}
