import { useCallback, useMemo, type ReactNode } from 'react'
import { useAttention } from '../agent/attention'
import { NeedsYouContext, needsYouItems, notListed, useGrantRequests, type NeedsYou } from '../lib/needsYou'

/**
 * #6399 — the shell's "Needs you" read, as `AppShell` builds it, over the msw mocks: the agent's
 * pending input and the flow grant requests. For tests of the bell and the Activity section.
 */
export function NeedsYouHarness({ children, openSession = null }: { children: ReactNode; openSession?: NeedsYou['openSession'] }) {
  const attention = useAttention(true)
  const { grants, refresh: refreshGrants } = useGrantRequests()
  const counts = attention.counts
  const refreshAttention = attention.refresh
  const refresh = useCallback(() => {
    refreshAttention()
    refreshGrants()
  }, [refreshAttention, refreshGrants])
  const value = useMemo<NeedsYou>(
    () => ({
      // From whichever read has answered, as `AppShell` builds it.
      items: counts || grants ? needsYouItems(counts?.waiting ?? [], grants, []) : null,
      more: notListed(grants),
      refresh,
      openSession,
    }),
    [counts, grants, refresh, openSession],
  )
  return <NeedsYouContext.Provider value={value}>{children}</NeedsYouContext.Provider>
}
