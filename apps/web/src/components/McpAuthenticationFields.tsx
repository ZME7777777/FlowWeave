import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { McpAuthenticationWrite, WebsiteCredential } from '../types';
import { LaunchOptionMenu } from './LaunchOptionMenu';

function credentialMatchesMcp(credential: WebsiteCredential, address: string) {
  try {
    const url = new URL(address);
    const authority = address.match(/^https?:\/\/([^/?#]*)/i)?.[1];
    if (!authority || authority.includes('@')) return false;
    const explicitPort = authority.match(/:(\d+)$/)?.[1];
    const host = `${url.hostname.toLowerCase()}${explicitPort ? `:${Number(explicitPort)}` : ''}`;
    const target = credential.target_host.toLowerCase();
    let path = url.pathname;
    for (let index = 0; index < 4; index++) {
      const decoded = decodeURIComponent(path);
      if (decoded === path) break;
      path = decoded;
    }
    if (path.includes('\\') || path.split('/').some(part => part === '.' || part === '..')) return false;
    return credential.auth_type === 'TOKEN' && (host === target || credential.include_subdomains && host.endsWith(`.${target}`))
      && (credential.target_path === '/' || path === credential.target_path || path.startsWith(`${credential.target_path}/`));
  } catch { return false; }
}

export function McpAuthenticationFields({ value, url, disabled = false, onChange }: {
  value: McpAuthenticationWrite; url: string; disabled?: boolean;
  onChange: (value: McpAuthenticationWrite) => void;
}) {
  const credentials = useQuery({ queryKey: ['website-credentials'], queryFn: api.websiteCredentials, enabled: value.strategy === 'bearer' });
  const matches = (credentials.data ?? []).filter(item => credentialMatchesMcp(item, url));
  const selected = matches.find(item => item.id === value.credential_id);
  const options = matches.map(item => ({ value: item.id, label: `${item.name} · ${item.target_host}${item.target_path === '/' ? '' : item.target_path}` }));
  if (value.credential_id && !selected) options.unshift({ value: value.credential_id, label: '原认证不可用，请重新选择' });
  return <section className="mcp-authentication-fields full-row" aria-label="MCP 认证配置">
    <label><span>认证方式</span><LaunchOptionMenu label="MCP 认证方式" value={value.strategy} options={[{ value: 'none', label: '无额外认证' }, { value: 'bearer', label: 'Bearer Token' }]} disabled={disabled} onChange={strategy => onChange({ ...value, strategy: strategy as McpAuthenticationWrite['strategy'], credential_id: null })}/></label>
    {value.strategy === 'bearer' && <label><span>关联认证 *</span><LaunchOptionMenu label="MCP 关联认证" value={value.credential_id ?? ''} options={options} disabled={disabled || credentials.isPending || credentials.isError} placeholder={credentials.isPending ? '正在读取认证…' : '选择 Token 认证'} onChange={credential_id => onChange({ ...value, credential_id })}/></label>}
    <p>{value.strategy === 'bearer' ? credentials.isError ? <><span role="alert">认证目录读取失败。</span><button type="button" className="ghost" onClick={() => void credentials.refetch()}>重试</button></> : !credentials.isPending && !matches.length ? '暂无匹配的 Token 认证，请先在认证管理中添加对应服务的认证。' : value.credential_id && !selected && !credentials.isPending ? <span role="alert">原认证已删除、类型不符或超出服务地址范围。</span> : '仅显示服务地址范围匹配的 Token；凭据由当前账号管理。' : '沿用服务本身的连接配置。认证绑定仅对当前账号生效。'}</p>
  </section>;
}
