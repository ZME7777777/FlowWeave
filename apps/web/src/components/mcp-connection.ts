import type { AgentSessionMcpReadiness } from '../types';

export function mcpConnectionLabel(result?: AgentSessionMcpReadiness): string {
  if (!result) return '未测试';
  if (result.state === 'READY') return '已连接';
  if (result.reason === 'RUNTIME_NOT_READY') return 'Agent 运行环境未就绪';
  if (result.reason === 'MCP_AUTHENTICATION_UNAVAILABLE') return '关联认证不可用';
  if (result.reason === 'MCP_AUTHENTICATION_SCOPE') return '认证范围与服务地址不匹配';
  if (result.reason === 'MCP_AUTHENTICATION_CHANGED') return '认证已更新，请新建会话';
  if (result.reason === 'TOOLS_MISSING') return '未返回历史查询工具';
  if (result.error_kind === 'timeout' || result.reason === 'MCP_TIMEOUT') return '连接超时';
  if (result.error_kind === 'connection') return '连接或认证失败';
  if (result.reason?.startsWith('AGENT_HISTORY_')) return '历史查询配置或版本校验失败';
  return '连接失败，请检查配置与认证';
}
