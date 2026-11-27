import { Suspense, lazy, useCallback, useState, type ReactNode } from 'react'
import type { Spool } from '../../api/filament'
import { spoolCode } from '../../lib/homeReturns'
import { toast } from '../../lib/toast'

// The shell is on every page: the dialog loads only when a person opens one.
const HomeReturnDialog = lazy(async () => ({ default: (await import('./HomeReturnDialog')).HomeReturnDialog }))

/**
 * #7167 — the one home-return dialog of "Needs you", owned by the shell (and the test harness),
 * as `useUnknownSpoolDialog` is: the row goes away once the prompt is answered, and the open
 * dialog must stay. It holds the spool as it was when the person opened it. Opening asks for a
 * fresh poll; a done move reads the list again.
 */
export function useHomeReturnDialog(refresh: () => void): {
  open: (spool: Spool, onDone?: () => void) => void
  dialog: ReactNode
} {
  const [opened, setOpened] = useState<{ spool: Spool; after: (() => void) | undefined } | null>(null)
  const close = useCallback((open: boolean) => {
    if (!open) setOpened(null)
  }, [])
  const dialog = opened && (
    <Suspense fallback={null}>
      <HomeReturnDialog
        spool={opened.spool}
        open
        onOpenChange={close}
        onDone={(moved, box) => {
          setOpened(null)
          refresh()
          toast({ title: `Put ${spoolCode(moved)} in ${box}` })
          opened.after?.()
        }}
      />
    </Suspense>
  )
  const open = useCallback(
    (spool: Spool, after?: () => void) => {
      setOpened({ spool, after })
      refresh()
    },
    [refresh],
  )
  return { open, dialog }
}
