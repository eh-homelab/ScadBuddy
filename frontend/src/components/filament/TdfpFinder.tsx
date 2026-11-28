import { SearchIcon } from 'lucide-react'
import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, newRequestId } from '../../api/client'
import { createTdfpFilament, searchTdfp, type TdfpCandidate, type TdfpCreated, type TdfpCreateRequest } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'

/** What the type's fields say the filament is: the search's and the add form's first values. */
export interface TdfpDefaults {
  brand: string
  material: string
  materialType: string
  colour: string
  /** `#RRGGBB` each, the type's colour first. */
  hexes: string[]
  nominalWeight: string
}

interface Props {
  defaults: TdfpDefaults
  /** The saved type whose `tdfp_id` a new filament becomes; null while the type is not saved or has an id. */
  linkTypeId: string | null
  /** The type has a saved 3DFP id: the add is not offered. */
  hasSavedId: boolean
  /** A match was chosen: the page puts it in the 3DFP id field, unsaved. */
  onUse: (tdfpId: number) => void
  /** A create for the linked type ended: the page reads the type again and answers the 3DFP id it holds. */
  onCreated: (tdfpId: number, typeTookIt: boolean) => Promise<number | null>
}

const detailOf = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail || fallback : fallback)

/** A create's refusal in plain words; one that may have saved says to search before trying again. */
function createProblem(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 502)
    return `${cause.detail || '3DFP failed while it saved'}. The filament may exist now: search before you add it again.`
  if (cause instanceof ApiError && cause.status === 504)
    return 'No answer came in time. The add may still run: press the add button again to wait for it, not to add a second one.'
  return detailOf(cause, 'The filament could not be added. Try again.')
}

function Field({ id, label, value, onChange, required, hint }: { id: string; label: string; value: string; onChange: (next: string) => void; required?: boolean; hint?: string }) {
  return (
    <div className="grid gap-1">
      <Label htmlFor={id} className="text-[12px] font-normal text-muted-foreground">
        {label}
      </Label>
      <Input id={id} required={required} className="h-8" value={value} onChange={(event) => onChange(event.target.value)} aria-describedby={hint ? `${id}-hint` : undefined} />
      {hint && (
        <p id={`${id}-hint`} className="text-[11px] text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  )
}

function Swatches({ hexes }: { hexes: string[] }) {
  return (
    <span className="flex shrink-0 gap-0.5" aria-hidden>
      {hexes.map((hex, at) => (
        <span key={at} className="size-4 rounded-sm border" style={{ backgroundColor: hex }} />
      ))}
    </span>
  )
}

/** One 3DFP filament: its colours, name, what it is, and its id. */
function CandidateRow({ one, children, note }: { one: TdfpCandidate; children?: ReactNode; note?: string }) {
  const what = [one.brand, one.material, one.material_type].filter(Boolean).join(', ')
  return (
    <li aria-label={`${one.name}, 3DFP ${one.tdfp_id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t py-1.5 first:border-t-0">
      <Swatches hexes={one.hexes ?? []} />
      <span className="min-w-0 flex-1">
        <span className="font-medium">{one.name}</span>
        <span className="block text-[12px] text-muted-foreground">
          {what}
          {one.nominal_weight_g != null && `, ${one.nominal_weight_g} g`}
          {`, 3DFP ${one.tdfp_id}`}
          {note && <span className="text-warn">{`: ${note}`}</span>}
        </span>
      </span>
      {children}
    </li>
  )
}

/**
 * #7025 — finds the type's filament on 3dfilamentprofiles.com (`POST /filament/tdfp/search`, on the
 * profile-fetch worker) and puts a match's id in the type's 3DFP id field, unsaved. When 3DFP has no
 * match it adds the filament (`POST /filament/tdfp/filaments`, once per key): a saved type with no id
 * takes the new one. A create that finds a likely duplicate saves nothing and lists it; the person
 * uses it, or marks it as another filament and adds anyway.
 */
export function TdfpFinder({ defaults, linkTypeId, hasSavedId, onUse, onCreated }: Props) {
  const [open, setOpen] = useState(false)
  const [ask, setAsk] = useState({ brand: defaults.brand, material: defaults.material, materialType: defaults.materialType, colour: defaults.colour })
  const [searching, setSearching] = useState(false)
  const [found, setFound] = useState<TdfpCandidate[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [add, setAdd] = useState({ ...ask, hexes: defaults.hexes.join(', '), nominalWeight: defaults.nominalWeight })
  const [sending, setSending] = useState(false)
  const [duplicate, setDuplicate] = useState<TdfpCreated['create'] | null>(null)
  const [differ, setDiffer] = useState<number[]>([])
  const [done, setDone] = useState<string | null>(null)
  /** One key per body: a retry of the same body follows the first create, a changed body is a new one. */
  const keyed = useRef<{ body: string; key: string } | null>(null)
  const asked = useRef(0)

  if (!open) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        onClick={() => {
          // The search starts from what the type's fields say now, unsaved edits too.
          setAsk({ brand: defaults.brand, material: defaults.material, materialType: defaults.materialType, colour: defaults.colour })
          setAdd({ brand: defaults.brand, material: defaults.material, materialType: defaults.materialType, colour: defaults.colour, hexes: defaults.hexes.join(', '), nominalWeight: defaults.nominalWeight })
          // The last time's matches and words belong to the fields it searched with.
          setFound(null)
          setError(null)
          setDone(null)
          setDuplicate(null)
          setDiffer([])
          setAdding(false)
          setOpen(true)
        }}
      >
        <SearchIcon /> Find on 3DFP
      </Button>
    )
  }

  async function search(event?: FormEvent) {
    event?.preventDefault()
    const at = ++asked.current
    setSearching(true)
    setError(null)
    setDone(null)
    try {
      const answer = await searchTdfp({
        brand: ask.brand.trim(),
        material: ask.material.trim() || null,
        material_type: ask.materialType.trim() || null,
        colour: ask.colour.trim() || null,
      })
      if (at === asked.current) setFound(answer.candidates)
    } catch (cause) {
      if (at === asked.current) setError(detailOf(cause, '3DFP could not be searched. Try again.'))
    } finally {
      if (at === asked.current) setSearching(false)
    }
  }

  /** An edit of the add form makes it another filament: it is checked again as a plain add. */
  function edit(next: typeof add) {
    setAdd(next)
    setDuplicate(null)
    setDiffer([])
  }

  function startAdd() {
    // The add starts from what was searched for.
    setAdd((was) => ({ ...was, brand: ask.brand, material: ask.material, materialType: ask.materialType, colour: ask.colour }))
    setDuplicate(null)
    setDiffer([])
    setAdding(true)
  }

  async function create(event?: FormEvent) {
    event?.preventDefault()
    const weight = add.nominalWeight.trim()
    if (weight && !/^\d+$/.test(weight)) {
      setError('Enter the nominal weight in grams, as a whole number.')
      return
    }
    const body: TdfpCreateRequest = {
      brand: add.brand.trim(),
      material: add.material.trim(),
      material_type: add.materialType.trim(),
      colour: add.colour.trim(),
      // 3DFP takes `#RRGGBB`: a typed alpha is cut off, as the type's own colours are.
      hexes: add.hexes.split(',').map((one) => one.trim().slice(0, 7).toUpperCase()).filter(Boolean),
      nominal_weight_g: weight ? Number(weight) : null,
      not_duplicates: duplicate ? differ : [],
      type_id: linkTypeId,
    }
    const text = JSON.stringify(body)
    if (keyed.current?.body !== text) keyed.current = { body: text, key: newRequestId() }
    setSending(true)
    setError(null)
    try {
      const answer = await createTdfpFilament(body, keyed.current.key)
      if (answer.create.status === 'duplicate') {
        setDuplicate(answer.create)
        setDiffer([])
        return
      }
      const id = answer.create.tdfp_id
      if (id == null) return
      setDuplicate(null)
      setAdding(false)
      if (answer.type_tdfp_id_set) {
        setDone(`Added to 3DFP as ${id}. The type’s 3DFP id is now ${id}.`)
        void onCreated(id, true)
      } else if (body.type_id) {
        // The type took an id another way while the add ran (R-7025-14): it keeps that one.
        const held = await onCreated(id, false)
        setDone(held == null ? `Added to 3DFP as ${id}.` : `Added to 3DFP as ${id}. The type already holds 3DFP id ${held}, so it keeps it.`)
      } else {
        setDone(`Added to 3DFP as ${id}. Save the type to keep the id.`)
        onUse(id)
      }
    } catch (cause) {
      setError(createProblem(cause))
    } finally {
      setSending(false)
    }
  }

  const reasons = new Map((duplicate?.likely ?? []).map((one) => [one.tdfp_id, one.reason]))
  // A duplicate answer that lists no filament still names the likely ids: each gets a row to tick.
  const listed = duplicate?.duplicates ?? []
  const dupes: TdfpCandidate[] =
    listed.length > 0
      ? listed
      : (duplicate?.likely ?? []).flatMap((one) => (one.tdfp_id == null ? [] : [{ tdfp_id: one.tdfp_id, name: `3DFP ${one.tdfp_id}` }]))

  return (
    <section aria-label="Find on 3DFP" className="flex flex-col gap-3 rounded-md border bg-muted/30 p-3 text-[13px]">
      <form onSubmit={(event) => void search(event)} className="flex flex-col gap-2">
        <div className="grid grid-cols-2 gap-2">
          <Field id="tdfp-brand" label="Brand" required value={ask.brand} onChange={(brand) => setAsk({ ...ask, brand })} />
          <Field id="tdfp-material" label="Material" value={ask.material} onChange={(material) => setAsk({ ...ask, material })} />
          <Field id="tdfp-type" label="Material type" value={ask.materialType} onChange={(materialType) => setAsk({ ...ask, materialType })} />
          <Field id="tdfp-colour" label="Colour" value={ask.colour} onChange={(colour) => setAsk({ ...ask, colour })} />
        </div>
        <div className="flex items-center gap-2">
          <Button type="submit" size="sm" disabled={searching || !ask.brand.trim()}>
            {searching ? <Spinner /> : <SearchIcon />} Search 3DFP
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
            Close
          </Button>
        </div>
      </form>

      {found && found.length > 0 && (
        <ul aria-label="3DFP matches" className="flex flex-col">
          {found.map((one) => (
            <CandidateRow key={one.tdfp_id} one={one}>
              <Button type="button" size="xs" variant="outline" onClick={() => onUse(one.tdfp_id)}>
                Use this id
              </Button>
            </CandidateRow>
          ))}
        </ul>
      )}
      {found && found.length === 0 && (
        <p className="text-muted-foreground">3DFP found no match. Its index misses some brands, so try fewer words before you add the filament.</p>
      )}

      {!hasSavedId && !adding && (
        <Button type="button" size="sm" variant="ghost" className="self-start" onClick={startAdd}>
          Add the missing filament
        </Button>
      )}

      {adding && (
        <form aria-label="Add to 3DFP" onSubmit={(event) => void create(event)} className="flex flex-col gap-2 border-t pt-3">
          <p className="text-muted-foreground">
            Adds the filament to 3dfilamentprofiles.com, where everyone can see it.{linkTypeId ? ' This type takes its new id.' : ''}
          </p>
          <div className="grid grid-cols-2 gap-2">
            <Field id="tdfp-add-brand" label="Brand" required value={add.brand} onChange={(brand) => edit({ ...add, brand })} />
            <Field id="tdfp-add-material" label="Material" required value={add.material} onChange={(material) => edit({ ...add, material })} />
            <Field id="tdfp-add-type" label="Material type" required value={add.materialType} onChange={(materialType) => edit({ ...add, materialType })} />
            <Field id="tdfp-add-colour" label="Colour name" required value={add.colour} onChange={(colour) => edit({ ...add, colour })} />
            <Field id="tdfp-add-hexes" label="Hexes" required hint="#RRGGBB each, 1 to 4, separated by commas." value={add.hexes} onChange={(hexes) => edit({ ...add, hexes })} />
            <Field id="tdfp-add-weight" label="Nominal weight (g)" value={add.nominalWeight} onChange={(nominalWeight) => edit({ ...add, nominalWeight })} />
          </div>
          {duplicate && (
            <div className="flex flex-col gap-1 rounded-md border border-warn/40 bg-warn/5 p-2">
              <p className="font-medium">Nothing was added: 3DFP may already have this filament.</p>
              {duplicate.duplicate_text && <p className="whitespace-pre-wrap text-muted-foreground">{duplicate.duplicate_text}</p>}
              {dupes.length === 0 && <p className="text-muted-foreground">3DFP named no filament. Search 3DFP for it, and use its id.</p>}
              <ul aria-label="3DFP may already have it" className="flex flex-col">
                {dupes.map((one) => (
                  <CandidateRow key={one.tdfp_id} one={one} note={reasons.get(one.tdfp_id)}>
                    <Button type="button" size="xs" variant="outline" onClick={() => onUse(one.tdfp_id)}>
                      Use this id
                    </Button>
                    <span className="flex items-center gap-1.5">
                      <Checkbox
                        id={`tdfp-differ-${one.tdfp_id}`}
                        aria-label="Not the same filament"
                        checked={differ.includes(one.tdfp_id)}
                        onCheckedChange={(on) => setDiffer((was) => (on ? [...was, one.tdfp_id] : was.filter((id) => id !== one.tdfp_id)))}
                      />
                      <Label htmlFor={`tdfp-differ-${one.tdfp_id}`} className="text-[12px] font-normal" aria-hidden>
                        Not the same
                      </Label>
                    </span>
                  </CandidateRow>
                ))}
              </ul>
            </div>
          )}
          <div className="flex items-center gap-2">
            {duplicate ? (
              <Button type="submit" size="sm" disabled={sending || differ.length === 0}>
                {sending && <Spinner />} Add anyway
              </Button>
            ) : (
              <Button type="submit" size="sm" disabled={sending}>
                {sending && <Spinner />} Add to 3DFP
              </Button>
            )}
            <Button type="button" size="sm" variant="ghost" disabled={sending} onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        </form>
      )}

      {done && <p className="font-medium">{done}</p>}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
