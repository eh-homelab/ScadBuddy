import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import {
  confirmTypeProposals,
  listTypeProposals,
  type TypeProposal,
  type TypeProposalResult,
  type TypeProposals as Proposals,
  type UngroupedSpool,
} from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { RgbaSwatch } from './RgbaSwatch'
import { SpoolLink } from './SpoolLink'

const SKIPPED: Record<string, string> = {
  not_found: 'it is gone',
  archived: 'it is archived',
  typed: 'it has a type now',
  changed: 'its material, colour or weight changed',
}

const MISSING: Record<UngroupedSpool['missing'][number], string> = {
  material: 'a material',
  rgba: 'a colour',
  label_weight_g: 'a label weight',
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`

/** A row's identity: two rows can name one existing type, so not its code. A spool is in one group. */
const rowKey = (group: TypeProposal) => group.spool_ids[0]!

const hexOf = (rgba: string) => (rgba.endsWith('FF') ? rgba.slice(0, 7) : rgba)

/**
 * What one confirm did, in one line: each type made or used, then each spool skipped. A row
 * that joined the type another row of this confirm just created says so, with the name it
 * sent (`sent`, by row), so a typed name is never dropped silently (R-6729-1).
 */
function doneText(result: TypeProposalResult, sent: string[], numbers: Map<string, number>): string {
  const lines: string[] = []
  const skipped: string[] = []
  for (const [at, group] of result.groups.entries()) {
    if (group.typed.length) {
      const spools = plural(group.typed.length, 'spool')
      const name = sent[at]
      lines.push(
        group.made
          ? `Created ${group.code} for ${spools}.`
          : group.new
            ? `Typed ${spools} as ${group.code}, which another row created${name && name !== group.code ? `; the name ${name} was not used` : ''}.`
            : `Typed ${spools} as ${group.code}.`,
      )
    }
    for (const one of group.skipped) skipped.push(`#${numbers.get(one.spool_id) ?? '?'}: ${SKIPPED[one.reason] ?? one.reason}`)
  }
  if (skipped.length) lines.push(`Skipped ${skipped.join('; ')}.`)
  return lines.join(' ') || 'No spool was typed.'
}

/**
 * #6701 — "Create types" on the Spools tab's Needs-type filter (spec §8.1): one row per type
 * ScadBuddy proposes for the spools that need one (`GET /filament/type-proposals`), with its
 * colour, its fields, how many spools it types and a name the person can change. "Create"
 * confirms one row, "Create all" every row, in one request. A row whose key a type already
 * matches uses that type. Below, the spools that lack a value a proposal needs. Nothing
 * shows while no spool needs a type. `onDone` gets what the confirm did.
 */
export function TypeProposals({ onDone }: { onDone: (text: string) => void }) {
  const [data, setData] = useState<Proposals | null>(null)
  const [names, setNames] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await listTypeProposals())
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The proposed types could not be loaded.')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (!data)
    return error ? (
      <p role="alert" className="text-[13px] text-destructive">
        {error}
      </p>
    ) : null
  if (!data.groups.length && !data.ungrouped.length) return null

  const nameOf = (group: TypeProposal) => names[rowKey(group)] ?? group.code

  const create = async (groups: TypeProposal[]) => {
    setBusy(true)
    setError(null)
    try {
      const picks = groups.map((group) => ({
        brand_id: group.brand_id,
        material_id: group.material_id,
        subtype_id: group.subtype_id,
        rgba: group.rgba,
        label_weight_g: group.label_weight_g,
        material_number: group.material_number,
        code: nameOf(group).trim(),
        spool_ids: group.spool_ids,
      }))
      const result = await confirmTypeProposals(picks)
      const numbers = new Map(groups.flatMap((group) => group.spool_ids.map((id, at) => [id, group.numbers[at]!] as const)))
      onDone(doneText(result, picks.map((pick) => pick.code), numbers))
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The types could not be created. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="type-proposals-title" className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-0.5">
          <h2 id="type-proposals-title" className="text-sm font-medium">
            Create types
          </h2>
          <p className="text-[13px] text-muted-foreground">
            Spools with the same brand, material, colour and label weight can share a type. Check each name, then create it.
          </p>
        </div>
        {data.groups.length > 1 && (
          <Button size="sm" disabled={busy} onClick={() => void create(data.groups)}>
            Create all {plural(data.groups.length, 'type')}
          </Button>
        )}
      </div>

      {error && (
        <p role="alert" className="text-[13px] text-destructive">
          {error}
        </p>
      )}

      {data.groups.length > 0 && (
        <ul aria-label="Proposed types" className="divide-y rounded-md border">
          {data.groups.map((group) => {
            const fields = [group.brand, group.material, group.subtype, group.colour_name ?? hexOf(group.rgba)].filter(Boolean).join(', ')
            const name = nameOf(group)
            return (
              <li key={rowKey(group)} aria-label={group.code} className="flex flex-wrap items-center gap-x-3 gap-y-2 p-2.5">
                <RgbaSwatch rgba={group.rgba} size="lg" />
                <div className="min-w-0 flex-1 basis-48 space-y-1">
                  <Input
                    aria-label="Type name"
                    value={name}
                    maxLength={128}
                    disabled={busy || group.type_id !== null}
                    onChange={(event) => setNames((current) => ({ ...current, [rowKey(group)]: event.target.value }))}
                  />
                  <p className="text-[13px] text-muted-foreground">{`${fields}, ${group.label_weight_g} g`}</p>
                  <p className="text-[13px]">
                    {`${plural(group.count, 'spool')}: ${group.numbers.map((n) => `#${n}`).join(', ')}`}
                    {group.type_id !== null && <span className="text-muted-foreground"> (type {group.code} exists and is used)</span>}
                  </p>
                </div>
                <Button size="sm" variant="outline" disabled={busy || !name.trim()} onClick={() => void create([group])}>
                  {group.type_id !== null ? 'Use type' : 'Create'}
                </Button>
              </li>
            )
          })}
        </ul>
      )}

      {data.ungrouped.length > 0 && (
        <div className="space-y-1">
          <h3 id="type-proposals-ungrouped" className="text-[13px] font-medium">
            Not enough to propose a type
          </h3>
          <ul aria-labelledby="type-proposals-ungrouped" className="space-y-0.5 text-[13px]">
            {data.ungrouped.map((one) => (
              <li key={one.spool_id}>
                <SpoolLink spool={{ id: one.spool_id }} className="underline underline-offset-2">
                  #{one.number}
                </SpoolLink>{' '}
                needs {one.missing.map((field) => MISSING[field]).join(' and ')}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
