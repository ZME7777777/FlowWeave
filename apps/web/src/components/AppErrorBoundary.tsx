import { AlertTriangle, RefreshCw } from 'lucide-react';
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

  render() {
    if (!this.state.error) return this.props.children;
    return <main className="app-recovery" role="alert">
      <AlertTriangle size={30}/>
      <h1>页面加载失败</h1>
      <p>页面遇到未预期异常。重新加载会使用最新页面代码和安全的导航上下文。</p>
      <details><summary>错误信息</summary><code>{this.state.error.message}</code></details>
      <div><button className="primary" onClick={this.reload}><RefreshCw size={14}/>重新加载</button></div>
    </main>;
  }
}
