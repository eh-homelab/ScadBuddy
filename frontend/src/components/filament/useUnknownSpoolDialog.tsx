import { Suspense, lazy, useCallback, useEffect, useState, type ReactNode } from 'react'
import type { UnknownSpoolTray } from '../../api/unknownTrays'
import { toast } from '../../lib/toast'
import type { NeedsYouItem } from '../../lib/needsYou'

// The shell is on every page: the dialog loads only when a person opens one.
const UnknownSpoolDialog = lazy(async () => ({ default: (await import('./UnknownSpoolDialog')).UnknownSpoolDialog }))

/**
 * #7143 — the one resolve dialog of "Needs you", owned by the shell (and the test harness), not by a
 * row: a row goes away when its tray does, and the open dialog must stay. It holds the tray as it
 * was when the person opened it. `gone` says that the list no longer has that tray (another client
 * linked it, or the slot was emptied): only after the shell's items have held the tray once since it
 * opened, because the shell's last poll may be older than the row. Opening asks for a fresh poll.
 */
export function useUnknownSpoolDialog(items: readonly NeedsYouItem[] | null, refresh: () => void): {
  open: (tray: UnknownSpoolTray, onResolved?: () => void) => void
  dialog: ReactNode
} {
  const [opened, setOpened] = useState<{ tray: UnknownSpoolTray; after: (() => void) | undefined } | null>(null)
  const tray = opened?.tray ?? null
  const listed = tray !== null && items !== null && items.some((item) => item.key === `unknown_spool:${tray.id}`)
  const [heldFor, setHeldFor] = useState<string | null>(null)
  useEffect(() => {
    if (tray && listed) setHeldFor(tray.id)
  }, [tray, listed])
  const gone = tray !== null && items !== null && heldFor === tray.id && !listed
  const close = useCallback((open: boolean) => {
    if (!open) setOpened(null)
  }, [])
  const dialog = tray && (
    <Suspense fallback={null}>
      <UnknownSpoolDialog
        tray={tray}
        gone={gone}
        open
        onOpenChange={close}
        onResolved={(resolved) => {
          setOpened(null)
          refresh()
          toast({ title: `Linked S-${resolved.number} to ${tray.slot}` })
          opened?.after?.()
        }}
      />
    </Suspense>
  )
  const open = useCallback(
    (next: UnknownSpoolTray, after?: () => void) => {
      setHeldFor(null)
      setOpened({ tray: next, after })
      refresh()
    },
    [refresh],
  )
  return { open, dialog }
}
