import { Activity, Boxes, BrainCircuit, GitFork, Hexagon, Library, PlayCircle, TerminalSquare } from 'lucide-react';
import { lazy, Suspense } from 'react';
import { FlowsPage } from './pages/FlowsPage';
import { NodesPage } from './pages/NodesPage';
import { ModelsPage } from './pages/ModelsPage';
import { RunsPage } from './pages/RunsPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { CapabilitiesPage } from './pages/CapabilitiesPage';
import { TerminalEnvironmentsPage } from './pages/TerminalEnvironmentsPage';
import { StandaloneAgentTerminal } from './components/AgentRuntimeSidebar';
import { useWorkbenchStore } from './store/workbench';

const AgentChatPage = lazy(async () => ({ default: (await import('./pages/AgentChatPage')).AgentChatPage }));

const nav = [
  { view: 'nodes' as const, label: '节点资产', icon: Boxes },
  { view: 'capabilities' as const, label: '能力仓库', icon: Library },
  { view: 'environments' as const, label: '终端环境', icon: TerminalSquare },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: PlayCircle },
  { view: 'models' as const, label: '大模型配置', icon: BrainCircuit },
];

export function App() {
  const { view, setView } = useWorkbenchStore();
  const terminalParams = new URLSearchParams(window.location.search);
  const terminalRunId = terminalParams.get('terminalRun');
  const terminalConversationId = terminalParams.get('terminalConversation');
  if (terminalRunId && terminalConversationId) return <StandaloneAgentTerminal runId={terminalRunId} conversationId={terminalConversationId}/>;
  if (view === 'agent-chat') return <div className="app-shell agent-focus-shell"><Suspense fallback={<div className="empty">加载 Agent 对话…</div>}><AgentChatPage/></Suspense></div>;
  return <div className="app-shell"><header className="topbar"><button className="brand" onClick={() => setView('nodes')}><Hexagon size={23} fill="currentColor"/>FlowWeave</button><nav>{nav.map(item => <button key={item.view} className={view === item.view ? 'active' : ''} onClick={() => setView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav><span className="kernel-status"><Activity size={14}/>产物驱动运行</span></header>
    <div className="principle-bar">一个 FlowRun 共享一个可替换 Runtime 与 Workspace；全部会话保留各自的 OpenHands 原生身份和事件树。</div>
    {view === 'nodes' && <NodesPage/>}{view === 'capabilities' && <CapabilitiesPage/>}{view === 'environments' && <TerminalEnvironmentsPage/>}{view === 'flows' && <FlowsPage/>}{view === 'runs' && <RunsPage/>}{view === 'models' && <ModelsPage/>}{view === 'workbench' && <WorkbenchPage/>}
  </div>;
}
