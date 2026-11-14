import { useMemo, useState } from 'react'
import { changedCount, settingText, withText } from '../../lib/processOverrides'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** The most rows drawn at once: a process preset has hundreds of keys; a search narrows them. */
export const SHOWN_ROWS = 200

type Props<T extends Record<string, unknown>> = {
  /** Names the disclosure, its fields and its test ids. */
  id: string
  title: string
  /** The disclosure button's test id; `<id>-toggle` when omitted. */
  toggleTestId?: string
  /** What this print sets, only what differs from `baseline`. */
  value: T | undefined
  /**
   * What each setting prints with when none is set: every key of the resolved process
   * preset (`POST /print/process-values`), with any template or slot values laid over
   * them. `null` while they are unknown: the list then holds only what is set.
   */
  baseline: Record<string, unknown> | null
  /** Why the values could not be read, said under the list; the editor still works. */
  unread?: string | null
  /** The values are being read. */
  loading?: boolean
  disabled?: boolean
  /** The disclosure opened: the dialog then reads the process's values. */
  onOpen?: () => void
  onChange: (next: T) => void
}

/**
 * #2998 — any Bambu Studio setting, by key (print-flow spec §4 "Any slicer setting").
 * Every key of the resolved process is listed with the value it prints with; a search
 * narrows them by key and "Changed only" shows what this print sets. A key the list lacks
 * is added by name. Nothing is refused: a key the process preset has no value for is sent
 * with a note, and the slicer judges it. Bambu Studio's labels and sections are compiled
 * into its binary and are not in the image, so each row shows its key.
 */
export function SlicerSettingsEditor<T extends Record<string, unknown>>({
  id,
  title,
  toggleTestId,
  value,
  baseline,
  unread = null,
  loading = false,
  disabled = false,
  onOpen,
  onChange,
}: Props<T>) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [changedOnly, setChangedOnly] = useState(false)
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  /** The field being typed in: its text stays as typed while it has the focus. */
  const [draft, setDraft] = useState<{ key: string; text: string } | null>(null)
  const set = useMemo(
    () => Object.entries(value ?? {}).filter(([, setting]) => setting !== null && setting !== undefined),
    [value],
  )
  const keys = useMemo(() => {
    const all = new Set([...Object.keys(baseline ?? {}), ...set.map(([key]) => key)])
    return [...all].sort((a, b) => a.localeCompare(b))
  }, [baseline, set])
  const needle = search.trim().toLowerCase()
  const matching = keys.filter(
    (key) => (!changedOnly || value?.[key] != null) && (needle === '' || key.toLowerCase().includes(needle)),
  )
  const rows = matching.slice(0, SHOWN_ROWS)
  const changed = changedCount(value)
  const update = (key: string, text: string | null) => onChange(withText(value, key, text, baseline?.[key]))

  function add() {
    const key = newKey.trim()
    if (key === '') return
    onChange(withText(value, key, newValue, baseline?.[key]))
    setNewKey('')
    setNewValue('')
    setSearch(key)
  }

  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) onOpen?.()
      }}
    >
      <CollapsibleTrigger asChild>
        <Button type="button" variant="link" size="sm" className="h-auto w-fit px-0 text-xs" data-testid={toggleTestId ?? `${id}-toggle`}>
          {title}
          {changed > 0 && <span className="text-muted-foreground">{` (${changed} changed)`}</span>}
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-2 grid gap-2" data-testid={id}>
          <div className="flex flex-wrap items-center gap-3">
            <Input
              type="search"
              aria-label="Search settings by key"
              placeholder="Search by key"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="h-8 w-56"
              data-testid={`${id}-search`}
            />
            <div className="flex items-center gap-1.5">
              <Checkbox
                id={`${id}-changed-only`}
                checked={changedOnly}
                onCheckedChange={(next) => setChangedOnly(next === true)}
                data-testid={`${id}-changed-only`}
              />
              <Label htmlFor={`${id}-changed-only`} className="text-xs font-normal">
                Changed only
              </Label>
            </div>
          </div>
          {loading && <p className="text-xs text-muted-foreground">Reading the process preset's settings…</p>}
          {unread && (
            <p className="text-xs text-muted-foreground" role="status">
              {`The process preset's values could not be read (${unread}). A setting can still be added by key.`}
            </p>
          )}
          <div className="max-h-72 overflow-y-auto rounded border" role="list" aria-label={`${title}: settings`}>
            {rows.length === 0 && (
              <p className="p-2 text-xs text-muted-foreground">
                {keys.length === 0 ? 'No settings to list yet. Add one by key below.' : 'No setting matches.'}
              </p>
            )}
            {rows.map((key) => {
              const own = value?.[key]
              const isSet = own !== null && own !== undefined
              const base = baseline?.[key]
              const known = baseline === null || key in baseline
              const fieldId = `${id}-${key}`
              return (
                <div key={key} role="listitem" className="grid gap-1 border-b px-2 py-1.5 last:border-b-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Label htmlFor={fieldId} className="min-w-0 flex-1 font-mono text-xs font-normal break-all">
                      {key}
                      {isSet && <span className="font-sans text-foreground"> · changed</span>}
                    </Label>
                    <Input
                      id={fieldId}
                      disabled={disabled}
                      value={draft?.key === key ? draft.text : isSet ? settingText(own) : ''}
                      placeholder={base === undefined ? 'Default' : settingText(base)}
                      onFocus={(event) => setDraft({ key, text: event.target.value })}
                      onChange={(event) => {
                        setDraft({ key, text: event.target.value })
                        update(key, event.target.value)
                      }}
                      onBlur={() => setDraft(null)}
                      className="h-7 w-40 text-xs"
                      data-testid={`${fieldId}-value`}
                    />
                    {isSet && (
                      <Button
                        type="button"
                        variant="link"
                        size="sm"
                        className="h-auto px-0 text-xs"
                        disabled={disabled}
                        onClick={() => {
                          setDraft(null)
                          update(key, null)
                        }}
                        data-testid={`${fieldId}-reset`}
                      >
                        Reset
                      </Button>
                    )}
                  </div>
                  {base !== undefined && (
                    <p className="text-[11px] text-muted-foreground">{`Process: ${settingText(base)}`}</p>
                  )}
                  {!known && (
                    <p className="text-[11px] text-warn" data-testid={`${fieldId}-unknown`}>
                      Not in the process preset: sent as set, and Bambu Studio judges whether it knows this key.
                    </p>
                  )}
                </div>
              )
            })}
          </div>
          {matching.length > rows.length && (
            <p className="text-xs text-muted-foreground">{`Showing ${rows.length} of ${matching.length}. Search to narrow them.`}</p>
          )}
          <div className="flex flex-wrap items-end gap-2">
            <div className="grid gap-1">
              <Label htmlFor={`${id}-new-key`} className="text-xs font-normal text-muted-foreground">
                Any setting by key
              </Label>
              <Input
                id={`${id}-new-key`}
                placeholder="e.g. skirt_loops"
                value={newKey}
                disabled={disabled}
                onChange={(event) => setNewKey(event.target.value)}
                className="h-8 w-48 font-mono text-xs"
                data-testid={`${id}-new-key`}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor={`${id}-new-value`} className="text-xs font-normal text-muted-foreground">
                Value
              </Label>
              <Input
                id={`${id}-new-value`}
                value={newValue}
                disabled={disabled}
                onChange={(event) => setNewValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    add()
                  }
                }}
                className="h-8 w-32 text-xs"
                data-testid={`${id}-new-value`}
              />
            </div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled || newKey.trim() === '' || newValue === ''}
              onClick={add}
              data-testid={`${id}-add`}
            >
              Set
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}
