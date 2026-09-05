import { Boxes, GitFork, Hexagon, LockKeyhole, LogIn, ShieldCheck, UserRound } from 'lucide-react';
import { FormEvent, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AuthUser } from '../types';

export function LoginScreen({ onLogin }: { onLogin: (user: AuthUser) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    setError('');
    try {
      onLogin(await api.login(username.trim(), password));
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : '登录失败，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return <main className="login-page">
    <div className="login-grid">
      <section className="login-hero" aria-labelledby="login-hero-title">
        <div className="login-brand"><span><Hexagon size={25} fill="currentColor"/></span><b>FlowWeave</b></div>
        <div className="login-hero-copy">
          <p className="eyebrow">AGENT OPERATIONS PLATFORM</p>
          <h1 id="login-hero-title">让每一次智能执行，<br/><em>清晰、可控、可追溯。</em></h1>
          <p>将节点能力、流程编排与 Agent 会话汇聚在同一工作空间，从设计到运行始终保持上下文连续。</p>
          <div className="login-capabilities" aria-label="平台能力">
            <span><Boxes size={15}/>节点与能力资产</span>
            <span><GitFork size={15}/>可视化流程编排</span>
            <span><ShieldCheck size={15}/>运行治理与审计</span>
          </div>
        </div>
        <p className="login-hero-foot"><i/>ARTIFACT-DRIVEN AGENT RUNTIME</p>
      </section>
      <section className="login-panel" aria-label="登录区域">
        <div className="login-card">
          <header>
            <div className="login-mark"><Hexagon size={29} fill="currentColor"/></div>
            <p className="eyebrow">WELCOME BACK</p>
            <h2>登录 FlowWeave</h2>
            <p className="login-copy">输入你的平台账号，继续进入工作空间。</p>
          </header>
          <form onSubmit={submit}>
            <label>账号<span className="login-input"><UserRound size={16}/><input aria-label="账号" autoComplete="username" autoFocus placeholder="请输入账号" value={username} onChange={event => setUsername(event.target.value)}/></span></label>
            <label>密码<span className="login-input"><LockKeyhole size={16}/><input aria-label="密码" type="password" autoComplete="current-password" placeholder="请输入密码" value={password} onChange={event => setPassword(event.target.value)}/></span></label>
            {error && <p className="login-error" role="alert">{error}</p>}
            <button className="login-submit" disabled={submitting || !username.trim() || !password}>
              <span>{submitting ? '正在登录…' : '进入工作空间'}</span><LogIn size={16}/>
            </button>
          </form>
          <p className="login-security"><ShieldCheck size={14}/>凭据仅用于本次平台身份验证</p>
        </div>
      </section>
    </div>
  </main>;
}
