import { Activity, Boxes, BrainCircuit, GitFork, Hexagon, PlayCircle } from 'lucide-react';
import { FlowsPage } from './pages/FlowsPage';
import { NodesPage } from './pages/NodesPage';
import { ModelsPage } from './pages/ModelsPage';
import { RunsPage } from './pages/RunsPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { AgentChatPage } from './pages/AgentChatPage';
import { useWorkbenchStore } from './store/workbench';

const nav = [
  { view: 'nodes' as const, label: '节点资产', icon: Boxes },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: PlayCircle },
  { view: 'models' as const, label: '大模型配置', icon: BrainCircuit },
];

export function App() {
  const { view, setView } = useWorkbenchStore();
  return <div className="app-shell"><header className="topbar"><button className="brand" onClick={() => setView('nodes')}><Hexagon size={23} fill="currentColor"/>FlowWeave</button><nav>{nav.map(item => <button key={item.view} className={view === item.view ? 'active' : ''} onClick={() => setView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav><span className="kernel-status"><Activity size={14}/>产物驱动运行 · 人工最终决策</span></header>
    <div className="principle-bar">节点资产定义能力，Flow Node 定义实例；运行以不可变快照、显式产物绑定与多 Attempt 保留完整历史。</div>
    {view === 'nodes' && <NodesPage/>}{view === 'flows' && <FlowsPage/>}{view === 'runs' && <RunsPage/>}{view === 'models' && <ModelsPage/>}{view === 'workbench' && <WorkbenchPage/>}{view === 'agent-chat' && <AgentChatPage/>}
  </div>;
}
