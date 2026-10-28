import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';

export const marketSourcesQueryKey = ['market-sources'];
export function useMarketSources(kind: 'PLUGIN' | 'SKILL') {
  const query = useQuery({ queryKey: marketSourcesQueryKey, queryFn: ({ signal }) => api.marketSources(signal), staleTime: 0 });
  const [selectedId, setSelectedId] = useState('');
  const sources = query.data?.sources.filter(item => item.enabled && (kind === 'PLUGIN' ? item.kind === 'PLUGIN_GIT' : item.kind !== 'PLUGIN_GIT')) ?? [];
  const source = sources.find(item => item.id === selectedId) ?? sources[0];
  return { ...query, sources, source, setSelectedId };
}
