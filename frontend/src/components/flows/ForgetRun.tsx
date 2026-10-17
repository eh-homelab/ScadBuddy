import { useState } from 'react'
import { useNavigate } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { FlowRunStatus } from '../../api/types'
import { FLOW_TERMINAL } from '../../lib/flows'
import { Button } from '@/components/ui/button'
import { Confirm } from './Confirm'

/**
 * #2350 — forget a run (`DELETE /workflow-runs/{id}`): its sealed payload key first, so
 * its recorded history can never be read again, then its workflow (stopped if open) and
 * its row. A person's decision only: the route refuses an agent, and the confirmation is
 * user-only.
 */
export function ForgetRun({ runId, status }: { runId: string; status: FlowRunStatus }) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const ended = FLOW_TERMINAL.has(status)

  async function forget() {
    setBusy(true)
    setError(undefined)
    try {
      await api.deleteFlowRun(runId)
      void navigate('/workflows')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setBusy(false)
    }
  }

  const close = () => {
    if (busy) return
    setOpen(false)
    setError(undefined)
  }

  const label = ended ? 'Forget run' : 'Stop and forget'
  return (
    <Confirm
      trigger={
        <Button variant="destructive" size="sm">
          {label}
        </Button>
      }
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : close())}
      title="Forget this run?"
      action={label}
      busy={busy}
      error={error}
      onAction={() => void forget()}
    >
      {!ended && (
        <p className="text-destructive">
          This run has not finished. It is stopped first, and anything it waits on for you is dropped.
        </p>
      )}
      <p>
        Its steps, your answers and approvals, and its result are deleted, and so is the key its recorded history is
        sealed with: nobody can read that history again, here or in Temporal. This can&apos;t be undone.
      </p>
      <p>The flow itself stays registered. Prints or files the run already made are not touched.</p>
    </Confirm>
  )
}
