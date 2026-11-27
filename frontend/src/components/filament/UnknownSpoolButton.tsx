import { useContext, useState } from 'react'
import { ApiError } from '../../api/client'
import { listUnknownTrays } from '../../api/unknownTrays'
import { NeedsYouContext } from '../../lib/needsYou'
import { Button } from '@/components/ui/button'

/**
 * #7143 — opens the shell's one unknown-spool dialog for a tray. It reads the list on the click, so
 * the dialog holds the tray as it is now. Outside the shell (no dialog to open) it shows nothing.
 */
export function UnknownSpoolButton({ trayId, label = 'Tell ScadBuddy which spool…', onResolved }: { trayId: string; label?: string; onResolved?: () => void }) {
  const answerSpool = useContext(NeedsYouContext)?.answerSpool
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  if (!answerSpool) return null

  async function click() {
    setBusy(true)
    setNote(null)
    try {
      const tray = (await listUnknownTrays()).trays.find((one) => one.id === trayId)
      if (tray) answerSpool!(tray, onResolved)
      else setNote('That tray is no longer unknown.')
    } catch (cause) {
      setNote(cause instanceof ApiError ? cause.detail : 'ScadBuddy could not read the unknown spools. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void click()}>
        {label}
      </Button>
      {note && (
        <span role="status" className="text-warn">
          {note}
        </span>
      )}
    </span>
  )
}
