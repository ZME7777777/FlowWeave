import { Hexagon, LogIn } from 'lucide-react';
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
    <section className="login-card">
      <div className="login-mark"><Hexagon size={34} fill="currentColor"/></div>
      <p className="eyebrow">FLOWWEAVE PLATFORM</p>
      <h1>登录工作空间</h1>
      <p className="login-copy">使用部署管理员提供的账号继续。</p>
      <form onSubmit={submit}>
        <label>账号<input autoComplete="username" autoFocus value={username} onChange={event => setUsername(event.target.value)}/></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)}/></label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <button className="primary full" disabled={submitting || !username.trim() || !password}>
          <LogIn size={15}/>{submitting ? '正在登录…' : '登录'}
        </button>
      </form>
    </section>
  </main>;
}
