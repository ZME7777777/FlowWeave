import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { McpAuthenticationWrite } from '../types';
import { McpAuthenticationFields } from './McpAuthenticationFields';

export function McpAuthenticationPanel({ versionId, url, onSaved }: { versionId: string; url: string; onSaved: () => void }) {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ['mcp-authentication', versionId], queryFn: ({ signal }) => api.mcpAuthentication(versionId, signal), retry: false });
  const [draft, setDraft] = useState<McpAuthenticationWrite>();
  const save = useMutation({ mutationFn: () => api.saveMcpAuthentication(versionId, draft!), onSuccess: result => {
    qc.setQueryData(['mcp-authentication', versionId], result); setDraft(undefined); onSaved();
  } });
  if (query.isPending) return <div className="empty compact" role="status">正在读取认证绑定…</div>;
  if (query.isError) return <div className="empty compact" role="alert"><span>{query.error.message}</span><button type="button" className="secondary" onClick={() => void query.refetch()}>重试</button></div>;
  const current = query.data;
  if (!current.editable) return <p className="mcp-authentication-note">此 MCP 的认证由系统或已有协议配置管理。</p>;
  const value = draft ?? { strategy: current.strategy, credential_id: current.credential_id, expected_row_version: current.row_version };
  const dirty = value.strategy !== current.strategy || value.credential_id !== current.credential_id;
  return <div className="mcp-authentication-panel">
    <McpAuthenticationFields value={value} url={url} disabled={save.isPending} onChange={next => { save.reset(); setDraft(next); }}/>
    {!current.valid && !draft && <p role="alert">关联认证不可用，请重新选择后保存。</p>}
    {save.isError && <p role="alert">{save.error.message}</p>}
    <div className="mcp-authentication-actions"><span>{save.isSuccess ? '已保存，可重新测试连接。' : '用于新建会话和首次装配；已有会话保留原连接。'}</span><button type="button" className="primary" disabled={!dirty || save.isPending || value.strategy === 'bearer' && !value.credential_id} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存认证'}</button></div>
  </div>;
}
