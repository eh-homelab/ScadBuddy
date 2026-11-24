import { useState } from 'react'
import type { Filament } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { RgbaSwatch } from './RgbaSwatch'
import { linksBookRow, type CandidateDiff, type TypeCandidate } from './makeType'

const DIFF_WORDS: Record<CandidateDiff, string> = { material: 'material', subtype: 'subtype', effect: 'effect', alpha: 'opacity' }

/** `material and subtype`, `material, subtype and effect`. */
function joined(words: string[]): string {
  return words.length < 2 ? (words[0] ?? '') : `${words.slice(0, -1).join(', ')} and ${words.at(-1)}`
}

/** `PLA+, Silk, 250 g label, 3 spools`: what tells two types of one brand and colour apart. */
function detail(candidate: TypeCandidate, names: ReadonlyMap<string, string>, spoolsOf?: ReadonlyMap<string, number>): string {
  const { type } = candidate
  const count = spoolsOf ? (spoolsOf.get(type.id) ?? 0) : null
  return [
    names.get(type.material_id),
    type.subtype_id ? names.get(type.subtype_id) : null,
    `${type.label_weight_g} g label`,
    count == null ? null : `${count} ${count === 1 ? 'spool' : 'spools'}`,
  ]
    .filter(Boolean)
    .join(', ')
}

/**
 * #7159 R-7159-3 — "Make a type" for a price book row whose brand and colour a type already
 * has: link one of those types (the exact match chosen first, else the first) or make a new
 * type anyway. Linking sets only the type's cost per kg (R-7159-4). A type whose names or
 * colour the price book does not match to the row (`linksBookRow`) cannot link it: the
 * dialog says so first, and its button sets that type's cost instead.
 */
export function LinkTypeDialog({
  row,
  label,
  candidates,
  names,
  spoolsOf,
  busy,
  onLink,
  onMake,
  onClose,
}: {
  row: Filament
  label: string
  candidates: TypeCandidate[]
  names: ReadonlyMap<string, string>
  spoolsOf?: ReadonlyMap<string, number>
  busy: boolean
  onLink: (candidate: TypeCandidate) => void
  onMake: () => void
  onClose: () => void
}) {
  const [chosen, setChosen] = useState(candidates[0]?.type.id ?? '')
  const picked = candidates.find((one) => one.type.id === chosen) ?? candidates[0]
  // A forecast only, before the click: `linksBookRow` copies the server's match,
  // `backend/scadbuddy/orders/store.py` `existing_filament`; change both together. After the
  // click, the PATCH answer's `filament_id` is the truth (`FilamentsEditor` `linkType`, R-7344-2).
  const links = picked ? linksBookRow(row, picked.type, names) : false
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>This filament may already have a type</DialogTitle>
          <DialogDescription>
            {label} has the brand and colour of {candidates.length === 1 ? 'this type' : 'these types'}. Link one to give it this row's cost of {row.cost_per_kg} per kg,
            or make a new type.
          </DialogDescription>
        </DialogHeader>
        <RadioGroup aria-label="Types of this brand and colour" value={picked?.type.id ?? ''} onValueChange={setChosen} className="gap-1">
          {candidates.map((candidate) => {
            const id = `link-type-${candidate.type.id}`
            return (
              <label
                key={candidate.type.id}
                htmlFor={id}
                className="flex cursor-pointer items-start gap-3 rounded-md border border-transparent px-2 py-2 has-data-checked:border-border has-data-checked:bg-muted/50"
              >
                <RadioGroupItem id={id} value={candidate.type.id} className="mt-0.5" />
                <RgbaSwatch rgba={candidate.type.rgba} size="sm" className="mt-px" />
                <span className="flex min-w-0 flex-col gap-0.5 text-[13px]">
                  <span className="font-medium break-all">{candidate.type.code}</span>
                  <span className="text-muted-foreground">{detail(candidate, names, spoolsOf)}</span>
                  {candidate.exact ? (
                    <span className="text-xs text-muted-foreground">Same material, subtype, colour and effect as this row</span>
                  ) : (
                    <span className="text-xs text-warn">Its {joined(candidate.differs.map((one) => DIFF_WORDS[one]))} {candidate.differs.length === 1 ? 'differs' : 'differ'} from this row</span>
                  )}
                </span>
              </label>
            )
          })}
        </RadioGroup>
        {picked && !links && (
          <p className="text-[13px] text-muted-foreground">
            The price book links a type by its material, subtype and brand names and its colour. {picked.type.code} does not match this row's, so this row still has
            no type. Setting its cost gives {row.cost_per_kg} per kg to its own price book row, which the price book adds if it has none.
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" variant="outline" disabled={busy} onClick={onMake}>
            Make a new type
          </Button>
          {picked && (
            <Button type="button" disabled={busy} onClick={() => onLink(picked)}>
              {links ? `Link ${picked.type.code}` : `Set ${picked.type.code}'s cost`}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
