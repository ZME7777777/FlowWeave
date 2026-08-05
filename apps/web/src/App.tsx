import { Activity, Boxes, BrainCircuit, GitFork, Hexagon, KeyRound, PlayCircle } from 'lucide-react';
import { FormEvent, useEffect, useState } from 'react';
import { FlowsPage } from './pages/FlowsPage';
import { NodesPage } from './pages/NodesPage';
import { ModelsPage } from './pages/ModelsPage';
import { RunsPage } from './pages/RunsPage';
import { WorkbenchPage } from './pages/WorkbenchPage';
import { useWorkbenchStore } from './store/workbench';
import { AUTH_REQUIRED_EVENT, type AuthRequestDetail, verifyHumanWriteToken } from './api/client';

const nav = [
  { view: 'nodes' as const, label: '节点资产', icon: Boxes },
  { view: 'flows' as const, label: '流程编排', icon: GitFork },
  { view: 'runs' as const, label: '流程运行', icon: PlayCircle },
  { view: 'models' as const, label: '大模型配置', icon: BrainCircuit },
];

function TokenDialog({ request, onClose }: { request: AuthRequestDetail; onClose: () => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await verifyHumanWriteToken(token);
      request.resolve();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '令牌验证失败');
    } finally {
      setSubmitting(false);
    }
  };
  const cancel = () => {
    request.reject(new Error('已取消人工令牌验证'));
    onClose();
  };
  return <div className="modal-backdrop"><form className="modal token-dialog" onSubmit={submit}>
    <header><div><KeyRound size={24}/><span className="eyebrow">HUMAN CONTROL PLANE</span><h2>验证人工操作令牌</h2></div></header>
    <p>只读访问无需登录。首次写操作需验证，令牌仅保存在当前标签页。</p>
    <label>会话令牌<input type="password" autoFocus required value={token} onChange={(event) => setToken(event.target.value)}/></label>
    {error && <p className="error">{error}</p>}
    <footer><button type="button" className="ghost" onClick={cancel}>取消</button><button className="primary" disabled={submitting}>{submitting ? '验证中…' : '验证并继续'}</button></footer>
  </form></div>;
}

export function App() {
  const { view, setView } = useWorkbenchStore();
  const [authRequest, setAuthRequest] = useState<AuthRequestDetail>();
  useEffect(() => {
    const requireAuthentication = (event: Event) => {
      setAuthRequest((event as CustomEvent<AuthRequestDetail>).detail);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
  }, []);
  return <div className="app-shell"><header className="topbar"><button className="brand" onClick={() => setView('nodes')}><Hexagon size={23} fill="currentColor"/>FlowWeave</button><nav>{nav.map(item => <button key={item.view} className={view === item.view ? 'active' : ''} onClick={() => setView(item.view)}><item.icon size={15}/>{item.label}</button>)}</nav><span className="kernel-status"><Activity size={14}/>产物驱动运行 · 人工最终决策</span></header>
    <div className="principle-bar">节点资产定义能力，Flow Node 定义实例；运行以不可变快照、显式产物绑定与多 Attempt 保留完整历史。</div>
    {view === 'nodes' && <NodesPage/>}{view === 'flows' && <FlowsPage/>}{view === 'runs' && <RunsPage/>}{view === 'models' && <ModelsPage/>}{view === 'workbench' && <WorkbenchPage/>}
    {authRequest && <TokenDialog request={authRequest} onClose={() => setAuthRequest(undefined)}/>}
  </div>;
}
