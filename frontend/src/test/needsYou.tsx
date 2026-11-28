import { useCallback, useMemo, type ReactNode } from 'react'
import { useAttention } from '../agent/attention'
import { NeedsYouContext, needsYouItems, notListed, useGrantRequests, type NeedsYou } from '../lib/needsYou'
import { useHomeReturns } from '../lib/homeReturns'
import { useUnknownSpools } from '../lib/unknownSpools'
import { useHomeReturnDialog } from '../components/filament/useHomeReturnDialog'
import { useUnknownSpoolDialog } from '../components/filament/useUnknownSpoolDialog'

/**
 * #6399 — the shell's "Needs you" read, as `AppShell` builds it, over the msw mocks: the agent's
 * pending input, the flow grant requests, the unknown trays and the home returns (#7167). For
 * tests of the bell and the Activity section.
 */
export function NeedsYouHarness({ children, openSession = null }: { children: ReactNode; openSession?: NeedsYou['openSession'] }) {
  const attention = useAttention(true)
  const { grants, refresh: refreshGrants } = useGrantRequests()
  const { data: unknown, refresh: refreshUnknown } = useUnknownSpools()
  const { spools: homes, refresh: refreshHomes } = useHomeReturns()
  const counts = attention.counts
  const refreshAttention = attention.refresh
  const refresh = useCallback(() => {
    refreshAttention()
    refreshGrants()
    refreshUnknown()
    refreshHomes()
  }, [refreshAttention, refreshGrants, refreshUnknown, refreshHomes])
  // From whichever read has answered, as `AppShell` builds it.
  const items = useMemo(
    () => (counts || grants || unknown || homes ? needsYouItems(counts?.waiting ?? [], grants, [], unknown?.trays, homes ?? []) : null),
    [counts, grants, unknown, homes],
  )
  const { open: answerSpool, dialog } = useUnknownSpoolDialog(items, refresh)
  const { open: answerHome, dialog: homeDialog } = useHomeReturnDialog(refresh)
  const value = useMemo<NeedsYou>(
    () => ({ items, more: notListed(grants), refresh, openSession, answerSpool, answerHome }),
    [items, grants, refresh, openSession, answerSpool, answerHome],
  )
  return (
    <NeedsYouContext.Provider value={value}>
      {children}
      {dialog}
      {homeDialog}
    </NeedsYouContext.Provider>
  )
}
