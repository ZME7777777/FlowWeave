export type OnFailAction = 'ESCALATE' | 'RERUN' | 'STOP';
export const ON_FAIL_ACTIONS: readonly OnFailAction[] = ['ESCALATE', 'RERUN', 'STOP'];
export const MAX_RERUN_ROUNDS = 3;

/** Read the author's declaration from a gate config; anything invalid is the default (ESCALATE). */
export function readOnFail(config: Record<string, unknown>): { action: OnFailAction; rounds: number } {
  const raw = config.on_fail as { action?: unknown; max_rounds?: unknown } | undefined;
  const action = ON_FAIL_ACTIONS.find(item => item === raw?.action) ?? 'ESCALATE';
  const rounds = typeof raw?.max_rounds === 'number' && Number.isInteger(raw.max_rounds) && raw.max_rounds >= 1 && raw.max_rounds <= MAX_RERUN_ROUNDS ? raw.max_rounds : 1;
  return { action, rounds };
}

/** Return a new config with the declaration applied; the default is stored as no field at all. */
export function writeOnFail(config: Record<string, unknown>, action: OnFailAction, rounds = 1): Record<string, unknown> {
  const next = { ...config };
  if (action === 'ESCALATE') delete next.on_fail;
  else next.on_fail = action === 'RERUN' ? { action, max_rounds: Math.min(Math.max(Math.trunc(rounds) || 1, 1), MAX_RERUN_ROUNDS) } : { action };
  return next;
}
