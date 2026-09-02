import type { CapabilityAsset } from '../types';

type CapabilityVersionIndex = ReadonlyMap<string, Pick<CapabilityAsset, 'capability_type' | 'capability_key'>>;

/**
 * Select one immutable capability version while retaining at most one version
 * for a capability lineage (type + key).  This intentionally has no product
 * count limit: the platform schema is the single authority for request-size
 * validation, and every session entry point must use this same replacement
 * rule.
 */
export function selectCapabilityVersion(
  selectedIds: string[],
  item: Pick<CapabilityAsset, 'id' | 'capability_type' | 'capability_key'>,
  versionsById: CapabilityVersionIndex,
): string[] {
  const sameCapabilityIds = selectedIds.filter(id => {
    const selected = versionsById.get(id);
    return selected?.capability_type === item.capability_type
      && selected.capability_key === item.capability_key;
  });
  return [...selectedIds.filter(id => !sameCapabilityIds.includes(id)), item.id];
}

/** Apply the same lineage replacement rule to a batch (for collections and
 * select-all actions) without introducing a client-side count limit. */
export function selectCapabilityVersions(
  selectedIds: string[],
  items: Iterable<Pick<CapabilityAsset, 'id' | 'capability_type' | 'capability_key'>>,
  versionsById: CapabilityVersionIndex,
): string[] {
  return Array.from(items).reduce(
    (current, item) => current.includes(item.id) ? current : selectCapabilityVersion(current, item, versionsById),
    selectedIds,
  );
}
