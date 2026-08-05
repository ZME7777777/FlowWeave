import { AlertTriangle, RefreshCw, RotateCcw } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props { children: ReactNode }
interface State { error?: Error }

export class AppErrorBoundary extends Component<Props, State> {
  state: State = {};

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('FlowWeave UI render failed', error, info.componentStack);
  }

  private reload = () => window.location.reload();

  private reset = () => {
    try { window.localStorage.removeItem('flowweave-workbench'); } catch { /* storage unavailable */ }
    window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;
    return <main className="app-recovery" role="alert">
      <AlertTriangle size={30}/>
      <h1>页面加载失败</h1>
      <p>可能是页面版本更新后保留了不兼容的浏览器状态。你无需进入 Chrome 设置，可直接在这里恢复。</p>
      <details><summary>错误信息</summary><code>{this.state.error.message}</code></details>
      <div><button className="secondary" onClick={this.reload}><RefreshCw size={14}/>重新加载</button><button className="primary" onClick={this.reset}><RotateCcw size={14}/>重置页面状态</button></div>
    </main>;
  }
}
