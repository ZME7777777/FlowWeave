import { LockKeyhole, LogIn, ShieldCheck, UserRound } from 'lucide-react';
import { FormEvent, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AuthUser } from '../types';

function FlowWeaveMark({ className = '' }: { className?: string }) {
  return <svg className={className} viewBox="0 0 40 40" fill="none" aria-hidden="true">
    <path d="M20 3.5 34.3 11.7v16.6L20 36.5 5.7 28.3V11.7L20 3.5Z" stroke="currentColor" strokeWidth="1.5"/>
    <path d="m12.4 15.2 7.6-4.4 7.6 4.4v9.6L20 29.2l-7.6-4.4v-9.6Z" stroke="currentColor" strokeWidth="1.5"/>
    <path d="m12.4 15.2 7.6 4.4 7.6-4.4M20 19.6v9.6" stroke="currentColor" strokeWidth="1.5"/>
    <circle cx="20" cy="10.8" r="1.9" fill="currentColor"/>
    <circle cx="12.4" cy="24.8" r="1.9" fill="currentColor"/>
    <circle cx="27.6" cy="24.8" r="1.9" fill="currentColor"/>
  </svg>;
}

export function LoginScreen({ onLogin }: { onLogin: (user: AuthUser) => Promise<void> }) {
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
      await onLogin(await api.login(username.trim(), password));
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : '登录失败，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return <main className="login-page">
    <div className="login-grid">
      <section className="login-hero" aria-label="FlowWeave 产品介绍">
        <svg className="login-weave" viewBox="0 0 960 720" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
          <path d="M-80 476C108 476 132 248 323 248c184 0 217 256 399 256 109 0 166-76 278-76"/>
          <path d="M-80 540c183 0 225-175 374-175 185 0 222 205 397 205 122 0 187-101 309-101"/>
          <path d="M-80 411c145 0 199-271 390-271 184 0 221 285 397 285 125 0 181-72 293-72"/>
        </svg>
        <div className="login-brand">
          <span><FlowWeaveMark/></span>
          <div><b>FlowWeave</b><small>AGENT WORKFLOW PLATFORM</small></div>
        </div>
        <div className="login-hero-copy">
          <p className="eyebrow">DESIGN · ORCHESTRATE · GOVERN</p>
          <h1>让方法成为 Agent 工作流<br/><em>一次编排，持续复用。</em></h1>
          <p>编排可复用的 Agent 能力，以不可变快照驱动每次运行，让执行过程始终可控、可追溯。</p>
          <div className="login-proof" aria-label="产品能力">
            <span><i/>可视化编排</span>
            <span><i/>agent驱动</span>
            <span><i/>运行全程追溯</span>
          </div>
        </div>
        <p className="login-hero-foot"><span>FLOWWEAVE</span><i/><span>BUILD WITH CONTROL</span></p>
      </section>

      <section className="login-panel" aria-label="登录区域">
        <div className="login-card">
          <header>
            <p className="eyebrow">WELCOME BACK</p>
            <h2>登录 FlowWeave</h2>
            <p className="login-copy">使用你的平台账号继续进入工作空间。</p>
          </header>
          <form onSubmit={submit}>
            <label>账号<span className="login-input"><UserRound size={15}/><input aria-label="账号" autoComplete="username" autoFocus placeholder="请输入账号" value={username} onChange={event => setUsername(event.target.value)}/></span></label>
            <label>密码<span className="login-input"><LockKeyhole size={15}/><input aria-label="密码" type="password" autoComplete="current-password" placeholder="请输入密码" value={password} onChange={event => setPassword(event.target.value)}/></span></label>
            {error && <p className="login-error" role="alert">{error}</p>}
            <button className="login-submit" disabled={submitting || !username.trim() || !password}>
              <span>{submitting ? '正在登录…' : '进入工作空间'}</span><LogIn size={15}/>
            </button>
          </form>
          <p className="login-security"><ShieldCheck size={13}/>凭据仅用于本次平台身份验证</p>
        </div>
      </section>
    </div>
  </main>;
}
