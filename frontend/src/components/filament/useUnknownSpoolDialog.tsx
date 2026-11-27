import { Suspense, lazy, useCallback, useState, type ReactNode } from 'react'
import type { UnknownSpoolTray } from '../../api/unknownTrays'
import type { NeedsYouItem } from '../../lib/needsYou'

// The shell is on every page: the dialog loads only when a person opens one.
const UnknownSpoolDialog = lazy(async () => ({ default: (await import('./UnknownSpoolDialog')).UnknownSpoolDialog }))

/**
 * #7143 — the one resolve dialog of "Needs you", owned by the shell (and the test harness), not by a
 * row: a row goes away when its tray does, and the open dialog must stay. It holds the tray as it
 * was when the person opened it. `gone` says that the list no longer has that tray (another client
 * linked it, or the slot was emptied).
 */
export function useUnknownSpoolDialog(items: readonly NeedsYouItem[] | null, refresh: () => void): { open: (tray: UnknownSpoolTray) => void; dialog: ReactNode } {
  const [tray, setTray] = useState<UnknownSpoolTray | null>(null)
  const gone = tray !== null && items !== null && !items.some((item) => item.key === `unknown_spool:${tray.id}`)
  const close = useCallback((open: boolean) => {
    if (!open) setTray(null)
  }, [])
  const dialog = tray && (
    <Suspense fallback={null}>
      <UnknownSpoolDialog
        tray={tray}
        gone={gone}
        open
        onOpenChange={close}
        onResolved={() => {
          setTray(null)
          refresh()
        }}
      />
    </Suspense>
  )
  return { open: setTray, dialog }
}
