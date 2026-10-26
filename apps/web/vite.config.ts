import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function casePreviewApi(): Plugin {
  const now = '2026-10-26T12:00:00Z';
  const workspace = { id: 'case-preview-workspace', display_name: '场景空间预览工作区', desired_state: 'RUNNING', updated_at: now };
  const item = {
    id: 'case-preview', name: '新产品上线准备', description: '汇总产品上线所需的流程、资料和协作事项。该说明用于验证右侧紧凑表单中的三行截断与查看原文交互。', objective: '在上线窗口前完成流程检查、相关资料确认，并建立后续的协调会话。',
    materials: [{ kind: 'LINK', name: '产品上线需求说明', value: 'https://example.test/launch-requirements' }], work_directory_id: null,
    session_config: { model_provider_id: 'preview-provider', model_name: 'preview-model', reasoning_effort: 'medium', capability_version_ids: [], credential_ids: [] },
    state: 'ACTIVE', control_conversation_binding_id: null, control_work_directory_id: null, row_version: 1,
    stages: [{ id: 'case-preview-stage', case_id: 'case-preview', stage_key: 'validation', name: '上线前流程检查', objective: '检查上线前置条件。', position: 0, flow_run_id: 'preview-run', execution_mode: 'STEPWISE', state: 'ACTIVE', created_at: now, updated_at: now }],
    flow_run_ids: ['preview-run'], tasks: [], control_plans: [], decision_proposals: [], created_at: now, updated_at: now,
  };
  const respond = (response: any, body: unknown, status = 200) => {
    response.statusCode = status;
    response.setHeader('Content-Type', 'application/json');
    response.end(status === 204 ? undefined : JSON.stringify(body));
  };
  return {
    name: 'flowweave-case-preview-api',
    configureServer(server) {
      server.middlewares.use((request: any, response: any, next: () => void) => {
        const url = new URL(request.url ?? '/', 'http://localhost');
        if (url.pathname.indexOf('/api/v1/') !== 0) return next();
        const path = url.pathname.slice('/api/v1'.length);
        if (path === '/auth/me') return respond(response, { id: 'case-preview-user', username: '场景预览', role: 'USER', can_use_agent_sessions: true });
        if (path === '/cases' && request.method === 'GET') return respond(response, [item]);
        if (path.indexOf('/cases/case-preview') === 0 && request.method !== 'GET') return respond(response, item);
        if (path === '/agent-workspaces/default') return respond(response, workspace);
        if (path === `/agent-workspaces/${workspace.id}/work-directories`) return respond(response, { root: { id: 'root', display_name: '默认工作区', working_directory: '/workspace' }, items: [] });
        if (path === '/model-providers') return respond(response, [{ id: 'preview-provider', name: '预览模型服务', available_for_nodes: true, models: [{ model_name: 'preview-model', enabled: true, is_default: true, default_reasoning_effort: 'medium', supported_reasoning_efforts: ['low', 'medium', 'high'] }] }]);
        if (path === '/website-credentials') return respond(response, []);
        if (path === '/flow-runs') return respond(response, [{ id: 'preview-run', name: '上线验证流程', run_no: 1, state: 'ACTIVE', runtime_status: 'ACTIVE', runtime_write_available: true }]);
        return respond(response, { error: { code: 'RESOURCE_NOT_FOUND', message: '预览模式未提供此接口。' } }, 404);
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const preview = env.VITE_CASE_PREVIEW === 'true';
  return {
    base: env.VITE_BASE_PATH || '/',
    plugins: [react(), ...(preview ? [casePreviewApi()] : [])],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://localhost:8080',
          changeOrigin: true,
          ws: true,
        },
      },
    },
  };
});
