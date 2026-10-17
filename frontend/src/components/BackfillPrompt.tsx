import { listNames, type OutputRef } from '../lib/arrange'
import { shortId } from '../lib/format'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'

/** #902 — the question asked before outputs saved before Arrange are re-rendered. */
export function BackfillPrompt({
  outputs,
  onConfirm,
  onCancel,
}: {
  outputs: OutputRef[]
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Card size="sm" role="group" aria-label="Re-render first" className="gap-2 px-3 text-[13px]">
      <p>Re-render {listNames(outputs)}, then arrange?</p>
      <div className="flex gap-2">
        <Button type="button" size="sm" onClick={onConfirm}>
          Re-render
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </Card>
  )
}

/** Where each re-render is, one line per output. */
export function BackfillProgress({ outputs, progress }: { outputs: OutputRef[]; progress: Record<string, string> }) {
  const shown = outputs.filter((o) => progress[o.id])
  if (shown.length === 0) return null
  return (
    <ul aria-label="Re-render progress" aria-live="polite" className="flex flex-col gap-0.5 text-[12px] text-faint">
      {shown.map((o) => (
        <li key={o.id}>
          {o.name ?? shortId(o.id)}: {progress[o.id]}
        </li>
      ))}
    </ul>
  )
}
