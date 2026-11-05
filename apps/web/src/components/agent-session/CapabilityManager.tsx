import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Boxes, Check, Eye, EyeOff, LockKeyhole, Plus, RefreshCw, Search, ShieldAlert, Square, SquareCheck, X } from 'lucide-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { api as agentWorkspaceApi, ApiError } from '../../api/client';
import type { AgentConversation, AgentConversationCredentialSyncEntry, AgentSessionCapability, AgentSessionMcpReadiness, CapabilityAsset, WebsiteCredential } from '../../types';
import { selectCapabilityVersion, selectCapabilityVersions } from '../../utils/capabilitySelection';
import { useEscapeClose } from '../useEscapeClose';
import { CapabilityCollectionFilter } from './CapabilityCollectionFilter';
import { useAgentSessionGateway, useAgentSessionHost } from './agent-session-context';
import { capabilityTypeIcons, capabilityTypeLabels, capabilityTypeFilters, type AgentCapabilityType } from './capabilityPresentation';
import '../../pages/agent-workbench.css';
import './session-configuration-layout.css';

type CapabilityChoice = Pick<CapabilityAsset, 'id' | 'capability_type' | 'capability_key'> & Partial<CapabilityAsset>;

function capabilityLineageIdentity(item: CapabilityChoice): string {
  // Frozen conversation entries only retain type and key; imports keep this
  // identity stable while publishing immutable versions.
  return `${item.capability_type}:${item.capability_key}`;
}


type DraftCredentialEntry = WebsiteCredential & { sync_state?: AgentConversationCredentialSyncEntry['sync_state'] };


export function CapabilityManager({ workspaceId, bindingId, conversationCapabilities, draftCapabilityIds, draftCredentialIds, configurationPreset = false, capabilitiesOnly = false, onDraftCapabilityIdsChange, onClose, onCreateEnhancedConversation, onDraftCredentialIdsChange, onSaveConfiguration, childParentId, credentialSyncDisabled = false }: {
  childParentId?: string; credentialSyncDisabled?: boolean;
  workspaceId: string; bindingId?: string; conversationCapabilities?: AgentSessionCapability[]; onClose: () => void;
  draftCapabilityIds?: string[]; draftCredentialIds?: string[]; configurationPreset?: boolean; capabilitiesOnly?: boolean;
  onDraftCapabilityIdsChange?: (ids: string[]) => void;
  onCreateEnhancedConversation?: (capabilityVersionIds: string[]) => void;
  onDraftCredentialIdsChange?: (credentialIds: string[]) => void;
  onSaveConfiguration?: (configuration: { capabilityVersionIds: string[]; credentialIds: string[] }) => Promise<void> | void;
}) {
  const gateway = useAgentSessionGateway();
  const api = useMemo(() => childParentId ? {
    ...gateway.api,
    credentialSync: (_workspaceId: string, childId: string) => agentWorkspaceApi.agentChildCredentials(childParentId, childId),
    synchronizeCredentials: (_workspaceId: string, childId: string, ids: string[]) => agentWorkspaceApi.syncAgentChildCredentials(childParentId, childId, ids),
  } : gateway.api, [childParentId, gateway.api]);
  const host = useAgentSessionHost();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<AgentCapabilityType | 'ALL'>('ALL');
  const [showSelectedOnly, setShowSelectedOnly] = useState(false);
  const [managerTab, setManagerTab] = useState<'CAPABILITIES' | 'CREDENTIALS'>('CAPABILITIES');
  const configurationId = useId();
  const configurationContent = useRef<HTMLDivElement>(null);
  const listScroll = useRef({ CAPABILITIES: 0, CREDENTIALS: 0 });
  const selectConfigurationTab = (next: 'CAPABILITIES' | 'CREDENTIALS') => {
    listScroll.current[managerTab] = configurationContent.current?.querySelector('.agent-capability-list')?.scrollTop ?? 0;
    setManagerTab(next);
    requestAnimationFrame(() => { const list = configurationContent.current?.querySelector('.agent-capability-list'); if (list) list.scrollTop = listScroll.current[next]; });
  };
  const [selectedCollectionIds, setSelectedCollectionIds] = useState<string[]>([]);
  const [chosenIds, setSelectedIds] = useState<string[]>(draftCapabilityIds ?? []);
  const [selectedCredentialIds, setSelectedCredentialIds] = useState<string[]>([]);
  const [mcpReadiness, setMcpReadiness] = useState<Record<string, AgentSessionMcpReadiness | undefined>>({});
  const [checkingMcpIds, setCheckingMcpIds] = useState<Set<string>>(new Set());
  const [registrationFailure, setRegistrationFailure] = useState<string | null>(null);
  const dialog = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const credentialSelectionInitialized = useRef(false);
  const managerActive = useRef(true);
  useEffect(() => {
    managerActive.current = true;
    return () => { managerActive.current = false; };
  }, []);
  useEffect(() => {
    const opener = returnFocus.current;
    closeButton.current?.focus();
    return () => { opener?.focus(); };
  }, []);
  const catalogQuery = useQuery({ queryKey: host.queryKey('capability-catalog'), queryFn: api.capabilities, refetchOnMount: 'always' });
  const selectedIds = useMemo(() => chosenIds.filter(id => !(catalogQuery.data ?? []).some(item => item.id === id && item.is_builtin)), [chosenIds, catalogQuery.data]);
  const collectionsQuery = useQuery({ queryKey: host.queryKey('capability-collections'), queryFn: api.capabilityCollections, enabled: !childParentId });
  const credentialQuery = useQuery({
    queryKey: host.queryKey('credential-sync', workspaceId, bindingId, childParentId),
    queryFn: () => api.credentialSync(workspaceId, bindingId!),
    enabled: Boolean(bindingId && managerTab === 'CREDENTIALS'),
  });
  const credentialCatalogQuery = useQuery({
    queryKey: ['website-credentials'],
    queryFn: agentWorkspaceApi.websiteCredentials,
    enabled: Boolean(!capabilitiesOnly && !bindingId && (managerTab === 'CREDENTIALS' || configurationPreset)),
  });
  useEffect(() => {
    if (credentialSelectionInitialized.current) return;
    if (bindingId && credentialQuery.data) {
      credentialSelectionInitialized.current = true;
      setSelectedCredentialIds(credentialQuery.data.credentials
        .filter(item => item.sync_state === 'NEW' || item.sync_state === 'MODIFIED')
        .map(item => item.id));
    } else if (!bindingId && credentialCatalogQuery.data) {
      credentialSelectionInitialized.current = true;
      setSelectedCredentialIds(
        draftCredentialIds ?? credentialCatalogQuery.data.map(credential => credential.id),
      );
    }
  }, [bindingId, credentialCatalogQuery.data, credentialQuery.data, draftCredentialIds]);
  const frozenCreationOnlyIds = useMemo(() => new Set(
    (conversationCapabilities ?? [])
      .filter(item => item.capability_type === 'CONTEXT' || item.capability_type === 'AGENT_DEFINITION' || item.capability_type === 'HOOK')
      .map(item => item.id),
  ), [conversationCapabilities]);
  const allCapabilities = useMemo(() => {
    const entries = new Map<string, CapabilityChoice>((catalogQuery.data ?? []).map(item => [item.id, item]));
    for (const item of conversationCapabilities ?? []) {
      // The frozen record supplies session state while the catalog retains
      // immutable presentation metadata such as its published revision.
      entries.set(item.id, { ...entries.get(item.id), ...item });
    }
    return entries;
  }, [catalogQuery.data, conversationCapabilities]);
  const previousSelection = useRef<string[] | null>(null);
  useEffect(() => {
    const current = bindingId ? conversationCapabilities : draftCapabilityIds?.map(id => ({ id }));
    if (!current) return;
    const next = current.filter(item => !('is_builtin' in item) || !item.is_builtin).map(item => item.id);
    const previous = previousSelection.current;
    if (previous && previous.length === next.length && previous.every((id, index) => id === next[index])) return;
    previousSelection.current = next;
    setSelectedIds(chosen => {
      if (!bindingId || !previous) return next;
      // Polling must not discard staged upgrades or failed items from a partial save.
      const pending = chosen.filter(id => !previous.includes(id));
      return selectCapabilityVersions(next, pending.map(id => allCapabilities.get(id)).filter((item): item is CapabilityChoice => Boolean(item)), allCapabilities);
    });
  }, [allCapabilities, bindingId, conversationCapabilities, draftCapabilityIds]);
  const registeredSkills = useMemo(() => new Map(
    (bindingId ? conversationCapabilities ?? [] : [])
      .filter(item => item.capability_type === 'SKILL')
      .map(item => [item.capability_key, item]),
  ), [bindingId, conversationCapabilities]);
  const skillUpdates = useMemo(() => {
    const updates = new Map<string, CapabilityAsset>();
    if (childParentId) return updates;
    for (const item of catalogQuery.data ?? []) {
      if (item.capability_type !== 'SKILL' || !item.is_latest) continue;
      const registered = registeredSkills.get(item.capability_key);
      if (!registered || registered.id === item.id) continue;
      const previousRevision = allCapabilities.get(registered.id)?.revision_number;
      if (previousRevision === undefined || item.revision_number > previousRevision) updates.set(item.capability_key, item);
    }
    return updates;
  }, [allCapabilities, catalogQuery.data, childParentId, registeredSkills]);
  const capabilities = useMemo<CapabilityChoice[]>(() => {
    if (childParentId) return (conversationCapabilities ?? []).map(item => allCapabilities.get(item.id) ?? item);
    const selectedByLineage = new Map<string, string>();
    for (const id of chosenIds) {
      const item = allCapabilities.get(id);
      if (item) selectedByLineage.set(capabilityLineageIdentity(item), id);
    }
    const visibleByLineage = new Map<string, CapabilityChoice>();
    for (const item of allCapabilities.values()) {
      if (!['SKILL', 'MCP', 'PLUGIN', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type)
        || item.is_builtin) continue;
      const lineage = capabilityLineageIdentity(item);
      const selectedId = selectedByLineage.get(lineage);
      const registeredSkill = registeredSkills.get(item.capability_key);
      const frozenCreationOnly = frozenCreationOnlyIds.has(item.id);
      const shown = item.capability_type === 'SKILL' && registeredSkill
        ? item.id === (selectedId ?? registeredSkill.id)
        : selectedId ? item.id === selectedId
          : item.is_latest || frozenCreationOnly;
      if (!shown || (bindingId && ['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) && !frozenCreationOnly)) continue;
      const existing = visibleByLineage.get(lineage);
      if (!existing || item.is_latest || selectedId === item.id) visibleByLineage.set(lineage, item);
    }
    return [...visibleByLineage.values()];
  }, [allCapabilities, bindingId, childParentId, chosenIds, conversationCapabilities, frozenCreationOnlyIds, registeredSkills]);
  const byId = allCapabilities;
  const selectedCollectionMemberIds = useMemo(() => new Set(
    (collectionsQuery.data ?? [])
      .filter(collection => selectedCollectionIds.includes(collection.id))
      .flatMap(collection => collection.members.flatMap(member => capabilities.filter(item => item.id === member.id
        || (item.capability_type === 'SKILL' && member.capability_type === 'SKILL' && item.capability_key === member.capability_key)).map(item => item.id))),
  ), [capabilities, collectionsQuery.data, selectedCollectionIds]);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return capabilities.filter(item =>
      (kind === 'ALL' || item.capability_type === kind)
      && (childParentId || !bindingId || !['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) || kind === item.capability_type)
      && (!showSelectedOnly || selectedIds.includes(item.id))
      && (!['ALL', 'SKILL'].includes(kind) || !selectedCollectionIds.length || selectedCollectionMemberIds.has(item.id))
      && (!needle || `${item.capability_key} ${item.description} ${item.filename}`.toLocaleLowerCase().includes(needle)),
    );
  }, [bindingId, capabilities, childParentId, kind, query, selectedCollectionIds.length, selectedCollectionMemberIds, selectedIds, showSelectedOnly]);
  const selectedMcpIds = useMemo(() => selectedIds.filter(id => byId.get(id)?.capability_type === 'MCP'), [byId, selectedIds]);
  const readonlyCreationOnly = Boolean(childParentId || bindingId && (kind === 'CONTEXT' || kind === 'AGENT_DEFINITION' || kind === 'HOOK'));
  const selectableVisibleIds = useMemo(() => visible
    .filter(item => !bindingId || !(conversationCapabilities ?? []).some(enabled => enabled.id === item.id
      || (enabled.capability_type === 'SKILL' && item.capability_type === 'SKILL' && enabled.capability_key === item.capability_key)))
    .map(item => item.id), [bindingId, conversationCapabilities, visible]);
  const allVisibleSelected = selectableVisibleIds.length > 0 && selectableVisibleIds.every(id => selectedIds.includes(id));
  const checkMcpReadiness = useCallback(async (ids: string[]) => {
    if (!ids.length) return [] as Array<readonly [string, AgentSessionMcpReadiness]>;
    setCheckingMcpIds(current => new Set([...current, ...ids]));
    const results = await Promise.all(ids.map(async id => {
      try {
        return [id, await api.mcpReadiness(workspaceId, id)] as const;
      } catch {
        return [id, { state: 'UNAVAILABLE', error_kind: 'unknown', checked_at: new Date().toISOString() } satisfies AgentSessionMcpReadiness] as const;
      }
    }));
    setMcpReadiness(current => ({ ...current, ...Object.fromEntries(results) }));
    setCheckingMcpIds(current => {
      const next = new Set(current);
      ids.forEach(id => next.delete(id));
      return next;
    });
    return results;
  }, [api, workspaceId]);
  useEffect(() => {
    if (!childParentId && !configurationPreset && selectedMcpIds.length) void checkMcpReadiness(selectedMcpIds);
  }, [checkMcpReadiness, childParentId, configurationPreset, selectedMcpIds]);
  const save = useMutation({
    mutationFn: async () => {
      if (childParentId) return undefined;
      if (configurationPreset && !bindingId) {
        const credentialIds = capabilitiesOnly ? [] : credentialSelectionInitialized.current
          ? selectedCredentialIds
          : draftCredentialIds ?? credentialCatalogQuery.data?.map(credential => credential.id);
        if (credentialIds === undefined) throw new Error('认证目录正在读取，请稍候再保存。');
        await onSaveConfiguration?.({
          capabilityVersionIds: selectedIds,
          credentialIds,
        });
        return selectedIds;
      }
      const readiness = await checkMcpReadiness(selectedMcpIds);
      const unavailable = readiness.find(([, status]) => status.state !== 'READY');
      if (unavailable) {
        const [id, status] = unavailable;
        const capability = byId.get(id);
        const reason = status.error_kind === 'timeout' ? '连接超时' : status.error_kind === 'connection' ? '无法连接' : '暂时不可用';
        throw new Error(`MCP「${capability?.capability_key ?? '未命名'}」${reason}，请重新检测后再保存。`);
      }
      if (!bindingId) return selectedIds;
      const loaded = new Set((conversationCapabilities ?? []).map(item => item.id));
      const pending = selectedIds.filter(id => !loaded.has(id));
      if (!pending.length) return undefined;
      return api.registerConversationCapabilities(workspaceId, bindingId, pending);
    },
    onMutate: () => setRegistrationFailure(null),
    onSuccess: value => {
      if (!managerActive.current) return;
      if (!bindingId) {
        onCreateEnhancedConversation?.(value as string[]);
        if (credentialSelectionInitialized.current) onDraftCredentialIdsChange?.(selectedCredentialIds);
        onClose();
        return;
      }
      if (!value) {
        onClose();
        return;
      }
      const registration = value as Awaited<ReturnType<typeof api.registerConversationCapabilities>>;
      void queryClient.invalidateQueries({ queryKey: host.queryKey('conversations', workspaceId) });
      queryClient.setQueryData<AgentConversation>(host.queryKey('conversation', workspaceId, bindingId), registration.conversation);
      if (registration.completed) {
        onClose();
        return;
      }
      const failed = byId.get(registration.failure?.capability_version_id ?? '');
      setRegistrationFailure(`已注册 ${registration.loaded_capability_version_ids.length} 项能力；${failed?.capability_key ?? '后续能力'} 未注册：${registration.failure?.message ?? '加载失败。'}`);
    },
  });
  const credentialSync = useMutation({
    mutationFn: () => {
      if (credentialSyncDisabled) throw new Error('当前子会话暂时无法同步认证');
      return api.synchronizeCredentials(workspaceId, bindingId!, selectedCredentialIds);
    },
    onSuccess: value => {
      setSelectedCredentialIds([]);
      queryClient.setQueryData(host.queryKey('credential-sync', workspaceId, bindingId, childParentId), value);
    },
  });
  useEscapeClose(() => { if (!save.isPending && !credentialSync.isPending) onClose(); });
  const updateChosenIds = (next: string[]) => { setSelectedIds(next); onDraftCapabilityIdsChange?.(next); };
  const toggle = (item: CapabilityChoice) => {
    if (chosenIds.includes(item.id)) {
      if (bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id
        || (enabled.capability_type === 'SKILL' && item.capability_type === 'SKILL' && enabled.capability_key === item.capability_key))) return;
      updateChosenIds(chosenIds.filter(id => id !== item.id));
    } else updateChosenIds(selectCapabilityVersion(chosenIds, item, byId));
  };
  const toggleVisible = () => updateChosenIds(allVisibleSelected ? chosenIds.filter(id => !selectableVisibleIds.includes(id))
    : selectCapabilityVersions(chosenIds, selectableVisibleIds.map(id => byId.get(id)).filter((item): item is CapabilityChoice => Boolean(item)), byId));
  const synchronizeSkill = (item: CapabilityAsset) => setSelectedIds(current => selectCapabilityVersion(current, item, byId));
  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab' || !(event.target instanceof Node) || !dialog.current?.contains(event.target)) return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])') ?? [])].filter(item => item.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  const busy = save.isPending || credentialSync.isPending;
  const credentialEntries = (bindingId ? credentialQuery.data?.credentials : credentialCatalogQuery.data) as DraftCredentialEntry[] | undefined;
  const credentialLoading = bindingId ? credentialQuery.isLoading : credentialCatalogQuery.isLoading;
  const credentialError = bindingId ? credentialQuery.error : credentialCatalogQuery.error;
  const selectableCredentialIds = (credentialEntries ?? []).filter(item => !bindingId || item.sync_state !== 'CURRENT').map(item => item.id);
  const allCredentialsSelected = selectableCredentialIds.length > 0 && selectableCredentialIds.every(id => selectedCredentialIds.includes(id));
  const toggleAllCredentials = () => setSelectedCredentialIds(current => allCredentialsSelected
    ? current.filter(id => !selectableCredentialIds.includes(id))
    : [...new Set([...current, ...selectableCredentialIds])]);
  const toggleCredential = (credential: DraftCredentialEntry) => setSelectedCredentialIds(current => current.includes(credential.id) ? current.filter(id => id !== credential.id) : [...current, credential.id]);
  return <div className="agent-capability-backdrop" role="presentation" onPointerDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section ref={dialog} className={`agent-capability-manager session-configuration-editor${bindingId ? ' has-session-tabs' : ''}${managerTab === 'CREDENTIALS' ? ' credentials-tab' : ''}`} role="dialog" aria-modal="true" aria-labelledby={`${configurationId}-title`} onKeyDown={trapFocus}>
      <header><h2 id={`${configurationId}-title`}>{childParentId ? '子会话配置' : '会话配置'}</h2><button ref={closeButton} type="button" aria-label={childParentId ? '关闭子会话配置' : '关闭会话配置'} disabled={busy} onClick={onClose}><X size={18}/></button></header>
      <div className="agent-session-configuration-layout">
      {!capabilitiesOnly && <nav className="agent-session-config-tabs" role="tablist" aria-label="会话配置分类" aria-orientation="vertical" onKeyDown={event => {
        const next = event.key === 'ArrowDown' || event.key === 'ArrowUp' ? managerTab === 'CAPABILITIES' ? 'CREDENTIALS' : 'CAPABILITIES' : event.key === 'Home' ? 'CAPABILITIES' : event.key === 'End' ? 'CREDENTIALS' : undefined;
        if (!next) return;
        event.preventDefault(); selectConfigurationTab(next);
        event.currentTarget.querySelector<HTMLButtonElement>(`[data-configuration-tab="${next}"]`)?.focus();
      }}>{(['CAPABILITIES', 'CREDENTIALS'] as const).map(tab => <button key={tab} id={`${configurationId}-${tab}`} type="button" role="tab" data-configuration-tab={tab} aria-selected={managerTab === tab} aria-controls={`${configurationId}-content`} tabIndex={managerTab === tab ? 0 : -1} onClick={() => selectConfigurationTab(tab)}>{tab === 'CAPABILITIES' ? <Boxes size={15}/> : <ShieldAlert size={15}/>}<span>{tab === 'CAPABILITIES' ? '能力' : '认证'}</span></button>)}</nav>}
      <div ref={configurationContent} id={`${configurationId}-content`} className="agent-session-configuration-content" role={!capabilitiesOnly ? 'tabpanel' : undefined} aria-labelledby={!capabilitiesOnly ? `${configurationId}-${managerTab}` : undefined}>
      {managerTab === 'CREDENTIALS' ? <div className="agent-credential-sync">
        <div className="agent-credential-sync-content">
          <div className="agent-credential-sync-heading"><button type="button" className="agent-capability-select-visible" aria-label={allCredentialsSelected ? '取消全选认证' : '全选认证'} title={allCredentialsSelected ? '取消全选认证' : '全选认证'} aria-pressed={allCredentialsSelected} disabled={busy || credentialLoading || Boolean(credentialError) || !selectableCredentialIds.length} onClick={toggleAllCredentials}>{allCredentialsSelected ? <SquareCheck size={16}/> : <Square size={16}/>}</button><span>认证名称</span><span>状态</span></div>
          <div className="agent-capability-list">{credentialLoading ? <p>正在读取认证信息…</p> : credentialError ? <p>{credentialError instanceof Error ? credentialError.message : '认证信息暂不可用。'}</p> : credentialEntries?.length ? credentialEntries.map(credential => {
            const selected = selectedCredentialIds.includes(credential.id);
            const current = Boolean(bindingId && credential.sync_state === 'CURRENT');
            const state = !bindingId ? '默认' : credential.sync_state === 'CURRENT' ? '已同步' : credential.sync_state === 'NEW' ? '有新增' : credential.sync_state === 'MODIFIED' ? '有修改' : '未记录';
            const stateClass = bindingId ? credential.sync_state?.toLocaleLowerCase() ?? 'unrecorded' : 'default';
            return <button type="button" key={credential.id} className={`${selected || current ? 'selected' : ''}${current ? ' locked' : ''}`} disabled={current || busy} aria-pressed={selected || current} aria-label={current ? `${credential.name}（已同步）` : undefined} title={current ? '该认证已同步到当前会话。' : undefined} onClick={() => toggleCredential(credential)}><i className="agent-selection-checkbox" aria-hidden="true">{selected || current ? <Check size={12}/> : null}</i><span className="agent-capability-icon credential" aria-hidden="true"><ShieldAlert size={16}/></span><span className="agent-capability-copy"><b>{credential.name}</b><small>{credential.target_host}{credential.target_path} · {credential.auth_type === 'TOKEN' ? 'Token' : '用户名密码'}</small></span><em className={`agent-credential-sync-state ${stateClass}`}>{state}</em></button>;
          }) : <p>当前没有可用认证信息。</p>}</div>
        </div>
        {credentialSync.error && <div className="agent-capability-error"><p>{credentialSync.error.message}</p></div>}
        <footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>{bindingId ? '关闭' : '取消'}</button>{bindingId ? <button type="button" className="primary" disabled={busy || credentialSyncDisabled || credentialLoading || selectedCredentialIds.length === 0} onClick={() => credentialSync.mutate()}><RefreshCw size={14}/>{credentialSync.isPending ? '正在同步…' : '同步认证'}</button> : <button type="button" className="primary" disabled={busy || credentialLoading} onClick={() => { if (configurationPreset && onSaveConfiguration) void save.mutate(); else { if (configurationPreset) onCreateEnhancedConversation?.(selectedIds); if (credentialSelectionInitialized.current) onDraftCredentialIdsChange?.(selectedCredentialIds); onClose(); } }}>保存认证配置</button>}</footer>
      </div> : <>
      <div className="agent-capability-typebar"><nav className="agent-capability-tabs" aria-label="能力类型">{capabilityTypeFilters.map(([value, label]) => <button type="button" key={value} className={kind === value ? 'active' : ''} aria-pressed={kind === value} onClick={() => setKind(value)}>{label}</button>)}</nav></div>
      <div className="agent-capability-toolbar" role="toolbar" aria-label="能力列表操作">
        {!readonlyCreationOnly && <button type="button" className="agent-capability-select-visible" aria-pressed={allVisibleSelected} aria-label={allVisibleSelected ? '取消选择筛选结果' : `选择筛选结果（${selectableVisibleIds.length}）`} title={allVisibleSelected ? '取消选择筛选结果' : `选择筛选结果（${selectableVisibleIds.length}）`} disabled={!selectableVisibleIds.length} onClick={toggleVisible}>{allVisibleSelected ? <SquareCheck size={16}/> : <Square size={16}/>}</button>}
        <button type="button" className={`agent-capability-show-selected${showSelectedOnly ? ' active' : ''}`} aria-label={showSelectedOnly ? '显示全部能力' : '只看已选'} title={showSelectedOnly ? '显示全部能力' : '只看已选'} aria-pressed={showSelectedOnly} onClick={() => setShowSelectedOnly(current => !current)}>{showSelectedOnly ? <Eye size={16}/> : <EyeOff size={16}/>}</button>
        {!childParentId && (kind === 'ALL' || kind === 'SKILL') && Boolean(collectionsQuery.data?.length) && <CapabilityCollectionFilter collections={collectionsQuery.data ?? []} selectedIds={selectedCollectionIds} onChange={setSelectedCollectionIds}/>}
        <label className="agent-capability-search"><Search size={15}/><input aria-label="搜索能力" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、说明或文件…"/></label>
      </div>
      <div className="agent-capability-list">{!childParentId && catalogQuery.isLoading ? <p>正在读取能力仓库…</p> : visible.length === 0 ? <p>{childParentId ? '没有匹配的冻结能力。' : readonlyCreationOnly ? `此会话创建时没有装配 ${kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'}。` : '没有匹配的已发布能力。'}</p> : visible.map(item => { const checked = selectedIds.includes(item.id);
        const isFrozenCreationOnly = Boolean(childParentId || bindingId && ['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) && frozenCreationOnlyIds.has(item.id));
        const registeredSkill = item.capability_type === 'SKILL' ? registeredSkills.get(item.capability_key) : undefined;
        const update = registeredSkill ? skillUpdates.get(item.capability_key) : undefined;
        const pendingUpdate = Boolean(registeredSkill && registeredSkill.id !== item.id);
        const locked = Boolean(registeredSkill || (bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id)));
        const isMcp = item.capability_type === 'MCP';
        const readiness = mcpReadiness[item.id];
        const checking = isMcp && checkingMcpIds.has(item.id);
        const readinessLabel = item.capability_type === 'CONTEXT' ? '系统上下文' : item.capability_type === 'AGENT_DEFINITION' ? 'Agent' : item.capability_type === 'HOOK' ? 'Hook' : !isMcp ? (item.capability_type === 'SKILL' ? '技能' : item.capability_type) : configurationPreset ? '运行时连接' : !checked ? 'MCP' : checking ? '检测中' : readiness?.state === 'READY' ? '已连接' : readiness?.error_kind === 'timeout' ? '连接超时' : readiness?.error_kind === 'connection' ? '连接失败' : '不可用';
        const detail = isMcp && checked && !configurationPreset && readiness?.state === 'UNAVAILABLE' ? `MCP ${readinessLabel}；请检测连接后保存。` : isMcp && readiness?.state === 'READY' && readiness.tools?.length ? `${readiness.tools.length} 个工具 · ${readiness.tools.join('、')}` : item.description || item.filename;
        const version = item.revision_number ? `v${item.revision_number}` : item.version ? item.version : '版本未知';
        const lockedLabel = isFrozenCreationOnly ? `${item.capability_key}（已装配）` : `${item.capability_key}（已注册）`;
        const Icon = capabilityTypeIcons[item.capability_type as AgentCapabilityType];
        const showMcpStatus = isMcp && checked && !configurationPreset && !childParentId;
        const choice = <button type="button" key={item.id} className={`${checked ? 'selected' : ''}${locked ? ' locked' : ''}`} disabled={locked || busy} aria-pressed={checked} aria-label={locked ? lockedLabel : undefined} onClick={() => toggle(item)}>
          <i className="agent-selection-checkbox" aria-hidden="true">{checked ? <Check size={12}/> : null}</i>
          <span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`} aria-hidden="true" title={capabilityTypeLabels[item.capability_type as AgentCapabilityType]}><Icon size={16}/></span>
          <span className="agent-capability-copy"><span className="agent-capability-name"><b title={item.capability_key}>{item.capability_key}</b><span className="agent-capability-version-badge" title={`能力版本 ${version}`}>{version}</span>{isFrozenCreationOnly && <span className="agent-capability-frozen-badge" title="此版本已随会话冻结，不能改为最新版本"><LockKeyhole size={10}/>已冻结</span>}{showMcpStatus && <em className={`mcp-status ${readiness?.state === 'READY' ? 'ready' : readiness?.state === 'UNAVAILABLE' ? 'unavailable' : 'checking'}`}>{readinessLabel}</em>}</span><small title={detail}>{detail || '暂无说明'}</small></span>
        </button>;
        return registeredSkill ? <div key={`SKILL:${item.capability_key}`} className={`agent-capability-skill-row${checked ? ' selected' : ''}${update ? ' has-update' : ''}`} data-capability-key={item.capability_key}>{choice}{update && <div className="agent-capability-skill-update"><span className={pendingUpdate ? 'pending' : ''}>{pendingUpdate ? '待保存' : '有新版本'}</span><button type="button" className="agent-capability-sync" disabled={busy || pendingUpdate} aria-label={`同步 ${item.capability_key} 到 v${update.revision_number}`} onClick={() => synchronizeSkill(update)}><RefreshCw size={12}/>{pendingUpdate ? '已同步' : '同步'}</button></div>}</div> : choice; })}</div>
      {(save.error || registrationFailure) && <div className="agent-capability-error"><p>{registrationFailure ?? save.error?.message}</p>{save.error instanceof ApiError && save.error.code === 'AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE' && onCreateEnhancedConversation && <div className="agent-capability-migration"><span>这条历史会话未在创建时注册原生能力市场。可新建一个空能力会话后，再按需选择要挂载的能力；此会话的历史内容会保留不变。</span><button type="button" className="secondary" disabled={save.isPending} onClick={() => onCreateEnhancedConversation([])}><Plus size={13}/>新建可使用能力的会话</button></div>}</div>}
      <footer>{!configurationPreset && !readonlyCreationOnly && selectedMcpIds.length > 0 && <button type="button" className="secondary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => void checkMcpReadiness(selectedMcpIds)}>重新检测 MCP</button>}<button type="button" className="secondary" disabled={save.isPending} onClick={onClose}>{readonlyCreationOnly ? '关闭' : '取消'}</button>{!readonlyCreationOnly && <button type="button" className="primary" disabled={save.isPending || checkingMcpIds.size > 0 || Boolean(configurationPreset && credentialCatalogQuery.isLoading)} onClick={() => save.mutate()}>{save.isPending ? '正在保存…' : bindingId ? '保存' : capabilitiesOnly ? '完成' : configurationPreset ? '保存会话配置' : '用于新建会话'}</button>}</footer></>}
      </div></div>
    </section>
  </div>;
}
