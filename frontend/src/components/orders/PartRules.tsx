import { useId, useState, type FormEvent } from 'react'
import { api } from '../../api/client'
import type { InventoryPart, PartRuleLine } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'

/** The Select's value for "no part": a Radix item cannot have an empty value. */
const NONE = '__none'

/** A part picker over the inventory; `allowNone` offers "None". */
export function PartSelect({ parts, value, onChange, label, allowNone = false }: { parts: InventoryPart[]; value: string | null; onChange: (id: string | null) => void; label: string; allowNone?: boolean }) {
  return (
    <Select value={value ?? NONE} onValueChange={(next) => onChange(next === NONE ? null : next)}>
      <SelectTrigger aria-label={label} className="w-48">
        <SelectValue placeholder="Pick a part" />
      </SelectTrigger>
      <SelectContent position="popper">
        {allowNone && <SelectItem value={NONE}>None</SelectItem>}
        {parts.map((p) => (
          <SelectItem key={p.id} value={p.id}>
            {p.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * A model's parts rule (#5543, §11): each line is one part per unit, the first colour
 * override the item has (in any case), else the default part (or none). A new item of the
 * model copies the parts it gives; items already added keep theirs.
 */
export function PartRules({ parts }: { parts: InventoryPart[] }) {
  const id = useId()
  const [slug, setSlug] = useState('')
  const [loaded, setLoaded] = useState<string | null>(null)
  const [lines, setLines] = useState<PartRuleLine[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  async function load(e: FormEvent) {
    e.preventDefault()
    const wanted = slug.trim()
    if (!wanted) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const rule = await api.partRule(wanted)
      setLines(rule.lines)
      setLoaded(wanted)
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    if (loaded === null) return
    setBusy(true)
    setError(null)
    try {
      const rule = await api.setPartRule(loaded, lines)
      setLines(rule.lines)
      setSaved(true)
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  function edit(index: number, change: Partial<PartRuleLine>) {
    setSaved(false)
    setLines((all) => all.map((line, i) => (i === index ? { ...line, ...change } : line)))
  }

  return (
    <section aria-label="Model rules" className="flex flex-col gap-3 rounded-md border p-3">
      <h2 className="font-medium">Model rules</h2>
      <p className="text-[13px] text-muted-foreground">The parts each unit of a model uses. A new line item of the model gets them; you can change them on the item.</p>
      <form onSubmit={load} className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-slug`}>Model slug</Label>
          <Input id={`${id}-slug`} value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="keychain" className="w-56" />
        </div>
        <Button type="submit" variant="outline" disabled={busy || !slug.trim()}>
          Open rule
        </Button>
      </form>
      {loaded !== null && (
        <div className="flex flex-col gap-2" data-testid="part-rule-lines">
          {lines.length === 0 && <p className="text-[13px] text-muted-foreground">{loaded} has no rule: its items get no parts until you add them.</p>}
          {lines.map((line, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-md border p-2">
              <div className="flex flex-wrap items-center gap-2">
                <Label htmlFor={`${id}-per-${index}`}>Per unit</Label>
                <Input
                  id={`${id}-per-${index}`}
                  inputMode="numeric"
                  className="w-20"
                  value={String(line.per_unit)}
                  onChange={(e) => edit(index, { per_unit: Math.max(1, Number(e.target.value) || 1) })}
                />
                <PartSelect parts={parts} value={line.part_id ?? null} onChange={(part_id) => edit(index, { part_id })} label={`Line ${index + 1} part`} allowNone />
                <Button size="xs" variant="ghost" onClick={() => edit(index, { when: [...(line.when ?? []), { colour: '#000000', part_id: parts[0]?.id ?? '' }] })} disabled={parts.length === 0}>
                  Add colour
                </Button>
                <Button size="xs" variant="ghost" aria-label={`Remove line ${index + 1}`} onClick={() => setLines((all) => all.filter((_, i) => i !== index))}>
                  Remove
                </Button>
              </div>
              {(line.when ?? []).map((w, wi) => (
                <div key={wi} className="flex flex-wrap items-center gap-2 pl-4 text-[13px]">
                  <span>If the item has</span>
                  <input
                    type="color"
                    aria-label={`Line ${index + 1} colour ${wi + 1}`}
                    value={w.colour.toLowerCase()}
                    onChange={(e) => edit(index, { when: (line.when ?? []).map((x, i) => (i === wi ? { ...x, colour: e.target.value } : x)) })}
                  />
                  <span>use</span>
                  <PartSelect parts={parts} value={w.part_id || null} onChange={(part) => part && edit(index, { when: (line.when ?? []).map((x, i) => (i === wi ? { ...x, part_id: part } : x)) })} label={`Line ${index + 1} colour ${wi + 1} part`} />
                  <Button size="xs" variant="ghost" onClick={() => edit(index, { when: (line.when ?? []).filter((_, i) => i !== wi) })}>
                    Remove colour
                  </Button>
                </div>
              ))}
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => setLines((all) => [...all, { per_unit: 1, part_id: parts[0]?.id ?? null, when: [] }])} disabled={parts.length === 0}>
              Add line
            </Button>
            <Button size="sm" onClick={() => void save()} disabled={busy}>
              Save rule
            </Button>
            {saved && <span role="status" className="text-[13px] text-muted-foreground">Saved.</span>}
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
