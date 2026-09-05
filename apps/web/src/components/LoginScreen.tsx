import { Hexagon, LockKeyhole, LogIn, ShieldCheck, UserRound } from 'lucide-react';
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
      <section className="login-hero" aria-label="Agent 流程动态视觉">
        <div className="agent-flow-visual" aria-hidden="true">
          <span className="agent-flow-aurora aurora-one"/>
          <span className="agent-flow-aurora aurora-two"/>
          <svg viewBox="0 0 760 680" focusable="false">
            <defs>
              <radialGradient id="agent-core-fill">
                <stop offset="0" stopColor="#f4ffac"/>
                <stop offset="0.36" stopColor="#d8f256"/>
                <stop offset="1" stopColor="#4e8a61" stopOpacity="0.15"/>
              </radialGradient>
              <linearGradient id="agent-flow-line" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#d8f256" stopOpacity="0"/>
                <stop offset="0.5" stopColor="#d8f256"/>
                <stop offset="1" stopColor="#7ee6b1" stopOpacity="0"/>
              </linearGradient>
              <filter id="agent-glow" x="-80%" y="-80%" width="260%" height="260%">
                <feGaussianBlur stdDeviation="8" result="blur"/>
                <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
              </filter>
            </defs>

            <g className="agent-orbit agent-orbit-outer">
              <ellipse cx="380" cy="340" rx="286" ry="214"/>
              <circle className="orbit-particle" cx="94" cy="340" r="5"/>
              <circle className="orbit-particle particle-dim" cx="666" cy="340" r="3"/>
            </g>
            <g className="agent-orbit agent-orbit-cross">
              <ellipse cx="380" cy="340" rx="246" ry="148"/>
              <circle className="orbit-particle" cx="626" cy="340" r="4"/>
            </g>
            <g className="agent-orbit agent-orbit-inner">
              <ellipse cx="380" cy="340" rx="166" ry="166"/>
              <circle className="orbit-particle particle-dim" cx="380" cy="174" r="4"/>
            </g>

            <g className="agent-flow-paths">
              <path d="M104 180 C220 154 250 270 326 307"/>
              <path d="M97 483 C201 510 244 430 322 375"/>
              <path d="M438 310 C516 260 575 180 674 214"/>
              <path d="M439 374 C520 420 573 502 677 465"/>
              <path d="M184 102 C260 146 305 198 347 276"/>
              <path d="M412 404 C438 500 498 553 574 594"/>
            </g>
            <g className="agent-flow-paths flow-path-highlight">
              <path d="M104 180 C220 154 250 270 326 307"/>
              <path d="M97 483 C201 510 244 430 322 375"/>
              <path d="M438 310 C516 260 575 180 674 214"/>
              <path d="M439 374 C520 420 573 502 677 465"/>
              <path d="M184 102 C260 146 305 198 347 276"/>
              <path d="M412 404 C438 500 498 553 574 594"/>
            </g>

            <g className="agent-node node-a"><circle r="25"/><circle r="7"/><path d="M-10 0h20M0-10v20"/></g>
            <g className="agent-node node-b"><rect x="-23" y="-23" width="46" height="46" rx="12"/><circle r="7"/><path d="M-12-9L12 9M-12 9L12-9"/></g>
            <g className="agent-node node-c"><polygon points="0,-28 25,-14 25,14 0,28 -25,14 -25,-14"/><circle r="7"/><circle r="14"/></g>
            <g className="agent-node node-d"><circle r="24"/><path d="M-11-7h22M-11 0h14M-11 7h18"/></g>
            <g className="agent-node node-e"><rect x="-22" y="-22" width="44" height="44" rx="22"/><path d="M-10 5L-2-5l7 7 8-12"/></g>
            <g className="agent-node node-f"><polygon points="0,-23 22,-7 14,19 -14,19 -22,-7"/><circle r="6"/></g>

            <g className="agent-core" filter="url(#agent-glow)">
              <circle className="core-wave core-wave-one" cx="380" cy="340" r="66"/>
              <circle className="core-wave core-wave-two" cx="380" cy="340" r="66"/>
              <circle className="core-shell" cx="380" cy="340" r="76"/>
              <polygon className="core-shape" points="380,282 430,311 430,369 380,398 330,369 330,311"/>
              <circle className="core-eye" cx="380" cy="340" r="21"/>
              <circle className="core-seed" cx="380" cy="340" r="8"/>
            </g>
          </svg>
          <span className="agent-flow-grain"/>
        </div>
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
