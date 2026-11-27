import { useCallback, useMemo, type ReactNode } from 'react'
import { useAttention } from '../agent/attention'
import { NeedsYouContext, needsYouItems, notListed, useGrantRequests, type NeedsYou } from '../lib/needsYou'
import { useUnknownSpools } from '../lib/unknownSpools'
import { useUnknownSpoolDialog } from '../components/filament/useUnknownSpoolDialog'

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
  // From whichever read has answered, as `AppShell` builds it.
  const items = useMemo(
    () => (counts || grants || unknown ? needsYouItems(counts?.waiting ?? [], grants, [], unknown?.trays) : null),
    [counts, grants, unknown],
  )
  const { open: answerSpool, dialog } = useUnknownSpoolDialog(items, refresh)
  const value = useMemo<NeedsYou>(
    () => ({ items, more: notListed(grants), refresh, openSession, answerSpool }),
    [items, grants, refresh, openSession, answerSpool],
  )
  return (
    <NeedsYouContext.Provider value={value}>
      {children}
      {dialog}
    </NeedsYouContext.Provider>
  )
}
