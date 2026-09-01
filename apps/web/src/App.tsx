import { Activity, Bot, Boxes, BrainCircuit, GitFork, Hexagon, Library, PlayCircle, TerminalSquare } from 'lucide-react';
import { useEffect, useState } from 'react';
import { FlowsPage } from './pages/FlowsPage';
import { NodesPage } from './pages/NodesPage';
import { ModelsPage } from './pages/ModelsPage';
import { RunsPage } from './pages/RunsPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { CapabilitiesPage } from './pages/CapabilitiesPage';
import { TerminalEnvironmentsPage } from './pages/TerminalEnvironmentsPage';
import { StandaloneAgentTerminal } from './components/AgentRuntimeSidebar';
import { AgentWorkbenchPage } from './pages/AgentWorkbenchPage';
import { FlowNodeSessionPage } from './pages/FlowNodeSessionPage';
import { useWorkbenchStore } from './store/workbench';

const nav = [
  { view: 'nodes' as const, label: '节点资产', icon: Boxes },
  { view: 'capabilities' as const, label: '能力仓库', icon: Library },
  { view: 'environments' as const, label: '终端环境', icon: TerminalSquare },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: PlayCircle },
  { view: 'models' as const, label: '大模型配置', icon: BrainCircuit },
  { view: 'agent-workbench' as const, label: 'Agent 会话', icon: Bot },
];

export function App() {
  const { view, setView } = useWorkbenchStore();
  const [, setRouteVersion] = useState(0);
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
        });
      }
      setRouteVersion(value => value + 1);
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  const navigate = (path: string, replace = false) => {
    if (replace) window.history.replaceState({}, '', path);
    else window.history.pushState({}, '', path);
    setRouteVersion(value => value + 1);
  };
  const nodeSessionRoute = window.location.pathname.match(
    /^\/flow-runs\/([^/]+)\/nodes\/([^/]+)\/attempts\/([^/]+)\/agent-sessions(?:\/([^/]+))?$/,
  );
  const isAgentRoute = window.location.pathname === '/agent' || window.location.pathname.startsWith('/agent/conversations/') || Boolean(nodeSessionRoute);
  const leaveAgentRoute = () => {
    if (isAgentRoute) navigate('/', true);
  };
  const selectView = (next: typeof view) => {
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
  if (terminalRunId && terminalConversationId) return <StandaloneAgentTerminal runId={terminalRunId} conversationId={terminalConversationId}/>;
  const renderedView = view === 'agent-workbench' ? 'nodes' : view;
  return <div className={`app-shell${isAgentRoute ? ' agent-workbench-shell' : ''}`}><header className="topbar"><button className="brand" onClick={() => selectView('nodes')}><Hexagon size={23} fill="currentColor"/>FlowWeave</button><nav>{nav.map(item => <button key={item.view} className={(nodeSessionRoute ? item.view === 'runs' : isAgentRoute ? item.view === 'agent-workbench' : renderedView === item.view) ? 'active' : ''} onClick={() => selectView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav><span className="kernel-status"><Activity size={14}/>{nodeSessionRoute ? 'FlowRun 节点会话' : isAgentRoute ? 'Agent 工作区' : '产物驱动运行'}</span></header>
    {isAgentRoute ? nodeSessionRoute ? <FlowNodeSessionPage flowRunId={decodeURIComponent(nodeSessionRoute[1])} nodeRunId={decodeURIComponent(nodeSessionRoute[2])} attemptId={decodeURIComponent(nodeSessionRoute[3])} onNavigate={navigate}/> : <AgentWorkbenchPage onNavigate={navigate}/> : <>{<div className="principle-bar">一个 FlowRun 共享一个可替换 Runtime 与 Workspace；全部会话保留各自的 OpenHands 原生身份和事件树。</div>}{renderedView === 'nodes' && <NodesPage/>}{renderedView === 'capabilities' && <CapabilitiesPage/>}{renderedView === 'environments' && <TerminalEnvironmentsPage/>}{renderedView === 'flows' && <FlowsPage/>}{renderedView === 'runs' && <RunsPage/>}{renderedView === 'models' && <ModelsPage/>}{renderedView === 'workbench' && <WorkbenchPage/>}</>}
  </div>;
}
