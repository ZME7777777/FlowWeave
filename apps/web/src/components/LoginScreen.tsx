import { Hexagon, LockKeyhole, LogIn, ShieldCheck, UserRound } from 'lucide-react';
import { FormEvent, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AuthUser } from '../types';

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
      <section className="login-hero" aria-label="Agent 流程动态视觉">
        <div className="agent-flow-visual" aria-hidden="true">
          <span className="agent-flow-aurora aurora-one"/>
          <span className="agent-flow-aurora aurora-two"/>
          <svg viewBox="0 0 1120 700" focusable="false">
            <defs>
              <radialGradient id="agent-core-fill">
                <stop offset="0" stopColor="#f4ffac"/>
                <stop offset="0.36" stopColor="#d8f256"/>
                <stop offset="1" stopColor="#4e8a61" stopOpacity="0.15"/>
              </radialGradient>
              <filter id="agent-glow" x="-80%" y="-80%" width="260%" height="260%">
                <feGaussianBlur stdDeviation="8" result="blur"/>
                <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
              </filter>
              <marker id="flow-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                <path d="M0 0L8 4 0 8Z"/>
              </marker>
            </defs>
            <path id="flow-route-motion" className="flow-route-base" d="M105 370C170 370 210 260 290 260S400 370 480 370 590 470 670 470 770 350 850 350 955 240 1030 240"/>
            <g className="flow-connectors">
              <path className="flow-link link-1" d="M162 345C205 322 225 275 241 267"/>
              <path className="flow-link link-2" d="M339 279C385 298 403 349 414 356"/>
              <path className="flow-link link-3" d="M546 397C587 417 608 456 620 461"/>
              <path className="flow-link link-4" d="M720 444C762 417 780 373 797 362"/>
              <path className="flow-link link-5" d="M903 325C947 299 966 258 984 249"/>
            </g>
            <g className="flow-packets" filter="url(#agent-glow)">
              <circle r="6"><animateMotion dur="8.4s" repeatCount="indefinite"><mpath href="#flow-route-motion"/></animateMotion></circle>
            </g>

            <g className="flow-stage stage-context stage-1">
              <rect className="stage-frame" x="45" y="310" width="120" height="120" rx="32"/>
              <path className="stage-icon" d="M77 348h46v38H77zM84 340h46v38M70 356v38h46"/>
              <circle className="stage-port" cx="105" cy="370" r="5"/>
            </g>
            <g className="flow-stage stage-capability stage-2">
              <polygon className="stage-frame" points="290,199 343,229 343,291 290,321 237,291 237,229"/>
              <circle className="stage-icon-fill" cx="290" cy="260" r="15"/>
              <path className="stage-icon" d="M290 245v-14M290 289v-14M275 260h-14M319 260h-14M279 249l-10-10M311 281l-10-10M301 249l10-10M269 281l10-10"/>
              <circle className="stage-port" cx="290" cy="229" r="5"/><circle className="stage-port" cx="321" cy="260" r="5"/><circle className="stage-port" cx="290" cy="291" r="5"/><circle className="stage-port" cx="259" cy="260" r="5"/>
            </g>
            <g className="flow-stage stage-agent stage-3">
              <rect className="stage-frame" x="410" y="300" width="140" height="140" rx="42"/>
              <circle className="core-wave core-wave-one" cx="480" cy="370" r="38"/>
              <circle className="core-wave core-wave-two" cx="480" cy="370" r="38"/>
              <polygon className="core-shape" points="480,329 515,349 515,391 480,411 445,391 445,349"/>
              <circle className="core-eye" cx="480" cy="370" r="14"/><circle className="core-seed" cx="480" cy="370" r="5"/>
            </g>
            <g className="flow-stage stage-gate stage-4">
              <rect className="stage-frame" x="615" y="415" width="110" height="110" rx="55"/>
              <path className="stage-icon gate-shield" d="M670 437l29 11v20c0 21-12 35-29 43-17-8-29-22-29-43v-20z"/>
              <path className="stage-icon" d="M657 471l9 9 19-21"/>
            </g>
            <g className="flow-stage stage-tools stage-5">
              <rect className="stage-frame" x="795" y="295" width="110" height="110" rx="28"/>
              <circle className="stage-icon" cx="850" cy="350" r="18"/>
              <circle className="stage-icon-fill" cx="850" cy="350" r="7"/>
              <path className="stage-icon" d="M850 322v10M850 368v10M822 350h10M868 350h10M830 330l8 8M862 362l8 8M870 330l-8 8M838 362l-8 8"/>
            </g>
            <g className="flow-stage stage-output stage-6">
              <circle className="stage-frame" cx="1030" cy="240" r="58"/>
              <path className="stage-icon" d="M1004 240l17 17 35-38M1006 275h48"/>
              <path className="stage-rays" d="M1030 166v-14M1030 328v-14M956 240h-14M1118 240h-14M978 188l-10-10M1092 302l-10-10M1082 188l10-10M968 302l10-10"/>
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
