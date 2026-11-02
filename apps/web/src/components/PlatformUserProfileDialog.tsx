import { useMutation, useQueryClient } from '@tanstack/react-query';
import './PlatformUserProfileDialog.css';
import { X } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api/client';
import type { PlatformUser } from '../types';
import { useEscapeClose } from './useEscapeClose';

export function PlatformUserProfileDialog({ user, onClose }: { user: PlatformUser; onClose: () => void }) {
  const client = useQueryClient();
  const [name, setName] = useState(user.display_name ?? '');
  const [email, setEmail] = useState(user.email ?? '');
  const save = useMutation({
    mutationFn: () => api.updatePlatformUserProfile(user.id, { display_name: name.trim(), email: email.trim() }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-directory'] }); onClose(); },
  });
  useEscapeClose(onClose, !save.isPending);
  return <div className="modal-backdrop product-dialog-backdrop">
    <section className="modal product-dialog platform-user-profile" role="dialog" aria-modal="true" aria-labelledby="platform-profile-title">
      <header><h2 id="platform-profile-title">编辑账户资料</h2><button type="button" className="ghost product-dialog-close" aria-label="关闭资料编辑" disabled={save.isPending} onClick={onClose}><X size={16}/></button></header>
      <p>{user.username} · 姓名和邮箱用于工作区默认 Git 提交身份。</p>
      <form onSubmit={event => { event.preventDefault(); save.mutate(); }}>
        <label>姓名<input autoFocus aria-label="姓名" value={name} maxLength={200} required disabled={save.isPending} onChange={event => setName(event.target.value)}/></label>
        <label>邮箱<input aria-label="邮箱" type="email" value={email} maxLength={320} required disabled={save.isPending} onChange={event => setEmail(event.target.value)}/></label>
        {save.isError && <p role="alert">{save.error.message}</p>}
        <footer><button type="button" className="secondary" disabled={save.isPending} onClick={onClose}>取消</button><button type="submit" className="primary" disabled={save.isPending || !name.trim() || !email.trim()}>{save.isPending ? '正在保存…' : '保存'}</button></footer>
      </form>
    </section>
  </div>;
}
