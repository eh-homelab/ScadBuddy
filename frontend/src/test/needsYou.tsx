import { useCallback, useMemo, type ReactNode } from 'react'
import { useAttention } from '../agent/attention'
import { NeedsYouContext, needsYouItems, notListed, useGrantRequests, type NeedsYou } from '../lib/needsYou'
import { useUnknownSpools } from '../lib/unknownSpools'

/**
 * #6399 — the shell's "Needs you" read, as `AppShell` builds it, over the msw mocks: the agent's
 * pending input and the flow grant requests. For tests of the bell and the Activity section.
 */
export function NeedsYouHarness({ children, openSession = null }: { children: ReactNode; openSession?: NeedsYou['openSession'] }) {
  const attention = useAttention(true)
  const { grants, refresh: refreshGrants } = useGrantRequests()
  const { data: unknown, refresh: refreshUnknown } = useUnknownSpools()
  const counts = attention.counts
  const refreshAttention = attention.refresh
  const refresh = useCallback(() => {
    refreshAttention()
    refreshGrants()
    refreshUnknown()
  }, [refreshAttention, refreshGrants, refreshUnknown])
  const value = useMemo<NeedsYou>(
    () => ({
      // From whichever read has answered, as `AppShell` builds it.
      items: counts || grants || unknown ? needsYouItems(counts?.waiting ?? [], grants, [], unknown?.trays) : null,
      more: notListed(grants),
      refresh,
      openSession,
    }),
    [counts, grants, unknown, refresh, openSession],
  )
  return <NeedsYouContext.Provider value={value}>{children}</NeedsYouContext.Provider>
}
