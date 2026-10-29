import { useState } from 'react'
import { aiCapabilities, type Access, type ItemView, type SourceView } from '../../api/capabilities'
import { ChoiceSelect } from '../settings/form'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { choicesOf, lowers, shownAccess } from './access'
import { ItemDetails } from './ItemDetails'
import { LowerConfirm } from './LowerConfirm'

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/**
 * One tool, hook, skill or subagent (#2171, spec §4.1): its one access select, the default
 * marked, Reset when a stored access takes effect, and the confirmation a lowering opens.
 * Every control is the agent's too (#4208): nothing here is user-only.
 */
export function ItemRow({ source, item, onSaved }: { source: string; item: ItemView; onSaved: (source: SourceView) => void }) {
  const [pending, setPending] = useState<Access | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const shown = shownAccess(item)

  async function save(write: () => Promise<SourceView>) {
    setBusy(true)
    setError(null)
    try {
      onSaved(await write())
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  const reset = () => void save(() => aiCapabilities.resetItem(source, item.id))
  const apply = (access: Access) => void save(() => aiCapabilities.setItem(source, item.id, access))

  function choose(value: string) {
    if (value === shown) return
    if (value === 'default' || value === item.default) return reset()
    const next = value as Access
    if (lowers(item, next)) setPending(next)
    else apply(next)
  }

  const note = item.fixed?.reason ?? (item.allowed !== null ? item.reason : null)

  return (
    <div className="flex flex-col gap-1.5 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{item.label}</div>
          {item.description !== null && <p className="line-clamp-2 text-xs text-muted-foreground">{item.description}</p>}
          <div className="sb-num text-xs text-muted-foreground">{item.harness_name}</div>
        </div>
        <div className="flex items-center gap-2">
          {busy && <Spinner />}
          <ChoiceSelect
            size="sm"
            className="w-48"
            aria-label={`Access of ${item.name}`}
            value={shown}
            onValueChange={choose}
            choices={choicesOf(item)}
            disabled={item.fixed !== null || busy}
          />
          {item.overridden && item.fixed === null && (
            <Button size="sm" variant="ghost" aria-label={`Reset ${item.name}`} onClick={reset} disabled={busy}>
              Reset
            </Button>
          )}
        </div>
      </div>
      {item.default_note !== null && <p className="text-xs text-muted-foreground">{item.default_note}</p>}
      {note !== null && <p className="text-xs text-muted-foreground">{note}</p>}
      {item.suggested !== null && item.suggested !== shown && (
        <p className="text-xs text-muted-foreground">{`server suggests ${item.suggested}`}</p>
      )}
      {item.missing && <p className="text-xs text-warn">Not in the last listing</p>}
      {error !== null && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
      {!item.missing && <ItemDetails source={source} item={item} />}
      {pending !== null && (
        <LowerConfirm
          open
          label={item.label}
          defaultAccess={item.default}
          from={shown}
          to={pending}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const next = pending
            setPending(null)
            apply(next)
          }}
        />
      )}
    </div>
  )
}
