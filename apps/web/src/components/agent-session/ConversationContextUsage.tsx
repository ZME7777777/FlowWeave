import type { CSSProperties } from 'react';
import type { AgentConversationContext } from '../../types';

const PRODUCT_CONTEXT_WINDOW_TOKENS = 512_000;
const CONTEXT_COMPACTION_TRIGGER_PERCENT = 80;
const CONTEXT_COMPACTION_TRIGGER_TOKENS = 409_600;

function exactCount(value: number): string {
  return value.toLocaleString('en-US');
}

export function ConversationContextUsage({ context, loading, error, usagePending }: {
  context?: AgentConversationContext;
  loading: boolean;
  error: boolean;
  usagePending?: boolean;
}) {
  // Show the product context window, not the lower native trigger used to
  // leave room for OpenHands to summarize. Provider-reported physical windows
  // are intentionally not displayed here because they are model-dependent.
  const visibleContextWindow = PRODUCT_CONTEXT_WINDOW_TOKENS;
  const currentContextTokens = context?.used_tokens;
  const hasCurrentContextUsage = typeof currentContextTokens === 'number' && currentContextTokens >= 0;
  const contextUsagePending = usagePending ?? (!hasCurrentContextUsage || context?.usage_current === false);
  const visibleContextTokens = hasCurrentContextUsage ? currentContextTokens : undefined;
  const contextProgress = typeof visibleContextWindow === 'number' && visibleContextWindow > 0
    && typeof visibleContextTokens === 'number'
    ? {
      used: visibleContextTokens,
      window: visibleContextWindow,
      usedLabel: exactCount(visibleContextTokens),
      windowLabel: exactCount(visibleContextWindow),
      percentage: Math.min(100, Math.round((visibleContextTokens / visibleContextWindow) * 100)),
    }
    : undefined;
  // This is a passive View indicator only. OpenHands owns the actual
  // condensation decision and the resulting event history.
  const currentViewEventCount = context?.view_event_count;
  const hasCurrentViewEventCount = typeof currentViewEventCount === 'number' && currentViewEventCount >= 0;
  const eventLimit = typeof context?.condenser_max_size === 'number'
    && context.condenser_max_size > 0
    ? context.condenser_max_size
    : 10_000;
  const eventProgress = hasCurrentViewEventCount
    ? Math.min(100, Math.round((currentViewEventCount / eventLimit) * 100))
    : undefined;
  const contextTitle = contextProgress
    ? `Token：OpenHands 当前 View ${contextProgress.used.toLocaleString()} / 上下文窗口 ${contextProgress.window.toLocaleString()}（${contextProgress.percentage}%）；达到 ${CONTEXT_COMPACTION_TRIGGER_PERCENT}%（${CONTEXT_COMPACTION_TRIGGER_TOKENS.toLocaleString()} Token）时自动压缩`
    : undefined;
  const tokenPendingLabel = loading
    ? '读取中'
    : error
      ? '暂不可用'
      : contextUsagePending
        ? '待模型更新'
        : '压缩阈值未知';
  const tokenPendingTitle = contextUsagePending
    ? '等待 OpenHands 返回当前活动 View 的 Token 用量。'
    : '等待当前会话提供自动压缩阈值。';
  const eventPendingLabel = loading
    ? '读取中'
    : error
      ? '暂不可用'
      : '待 Runtime 更新';
  const eventPendingTitle = '等待 OpenHands 返回压缩后活动 View 的正式事件数。';
  const activityTitle = hasCurrentViewEventCount
    ? `OpenHands 当前活动 View 事件 ${currentViewEventCount.toLocaleString()} / 自动压缩阈值 ${eventLimit.toLocaleString()}。`
    : undefined;
  return (
    <>
      {contextProgress ? (
        <span className="agent-context-progress token" title={contextTitle} aria-label={`Token 上下文用量 ${contextProgress.percentage}%`}>
          <i style={{ '--context-progress': `${contextProgress.percentage}%` } as CSSProperties} />
          <em><small>Token</small>{contextProgress.usedLabel} / {contextProgress.windowLabel}</em>
        </span>
      ) : (
        <span className="agent-context-progress token pending" title={tokenPendingTitle} aria-label={`Token 上下文用量${tokenPendingLabel}`}>
          <i style={{ '--context-progress': '0%' } as CSSProperties} />
          <em><small>Token</small>{tokenPendingLabel}</em>
        </span>
      )}
      {hasCurrentViewEventCount && eventProgress !== undefined ? (
        <span className="agent-context-progress activity events" title={activityTitle} aria-label={`OpenHands 当前活动 View 事件 ${currentViewEventCount} 条，自动压缩阈值 ${eventLimit} 条`}>
          <i style={{ '--context-progress': `${eventProgress}%` } as CSSProperties} />
          <em><small>事件</small>{exactCount(currentViewEventCount)} / {exactCount(eventLimit)}</em>
        </span>
      ) : (
        <span className="agent-context-progress activity events pending" title={eventPendingTitle} aria-label={`事件上下文用量${eventPendingLabel}`}>
          <i style={{ '--context-progress': '0%' } as CSSProperties} />
          <em><small>事件</small>{eventPendingLabel}</em>
        </span>
      )}
    </>
  );
}
