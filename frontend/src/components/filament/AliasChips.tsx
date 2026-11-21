import { PlusIcon, XIcon } from 'lucide-react'
import { useId, useState, type KeyboardEvent } from 'react'
import { ApiError } from '../../api/client'
import { setCatalogAliases, type AliasedKind } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

const CHIP = 'inline-flex max-w-full items-center gap-1 rounded-full border bg-background px-2 py-0.5 text-[12px] leading-4'

/** A row's aliases, read only, beside its name in the table. */
export function AliasList({ aliases }: { aliases: readonly string[] }) {
  if (aliases.length === 0) return null
  return (
    <span className="flex flex-wrap gap-1" aria-label="Aliases">
      {aliases.map((alias) => (
        <span key={alias} className={`${CHIP} text-muted-foreground`}>
          <span className="truncate">{alias}</span>
        </span>
      ))}
    </span>
  )
}

/**
 * #6706 — a row's aliases in its edit form: each a chip that can be removed, and a box to
 * add one. Each change is saved at once (`PUT /catalog/{kind}/{id}/aliases`); a refusal,
 * such as a key another row holds (409), shows under the box and keeps the typed text.
 */
export function AliasChips({ kind, id, aliases, onSaved }: { kind: AliasedKind; id: string; aliases: readonly string[]; onSaved: () => Promise<unknown> }) {
  const [adding, setAdding] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const hint = useId()

  async function save(next: string[]): Promise<boolean> {
    setBusy(true)
    setError(null)
    try {
      await setCatalogAliases(kind, id, next)
      await onSaved()
      return true
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The aliases could not be saved. Try again.')
      return false
    } finally {
      setBusy(false)
    }
  }

  async function add() {
    const alias = adding.trim()
    if (alias === '') return
    if (await save([...aliases, alias])) setAdding('')
  }

  function onKey(event: KeyboardEvent<HTMLInputElement>) {
    // Enter adds the alias; it must not submit the row's form.
    if (event.key !== 'Enter') return
    event.preventDefault()
    void add()
  }

  return (
    <div className="flex flex-col gap-1.5 sm:col-span-2">
      <span>Aliases</span>
      {aliases.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {aliases.map((alias) => (
            <li key={alias} className={`${CHIP} pr-0.5`}>
              <span className="truncate">{alias}</span>
              <button
                type="button"
                aria-label={`Remove alias ${alias}`}
                disabled={busy}
                className="rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50"
                onClick={() => void save(aliases.filter((one) => one !== alias))}
              >
                <XIcon className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex min-w-0 items-center gap-2">
        <Input
          aria-label="New alias"
          aria-describedby={hint}
          aria-invalid={error ? true : undefined}
          placeholder="Another name for it"
          value={adding}
          className="min-w-0 flex-1 text-[13px]"
          onChange={(event) => {
            setAdding(event.target.value)
            setError(null)
          }}
          onKeyDown={onKey}
        />
        <Button type="button" size="sm" variant="outline" disabled={busy || adding.trim() === ''} onClick={() => void add()}>
          <PlusIcon /> Add alias
        </Button>
      </div>
      <p id={hint} className={error ? 'text-warn' : 'text-[12px] text-muted-foreground'} role={error ? 'alert' : undefined}>
        {error ?? 'Imports find this row by any of these names. Each change saves at once.'}
      </p>
    </div>
  )
}
