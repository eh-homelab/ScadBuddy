import { SpoolLink } from './SpoolLink'
import { Checkbox } from '@/components/ui/checkbox'
import { cn } from '@/lib/utils'
import { RgbaSwatch } from './RgbaSwatch'
import { NeedsType } from './SpoolsTable'
import { grams, type SpoolGroup } from './spoolsQuery'

interface Props {
  groups: SpoolGroup[]
  selected: ReadonlySet<string>
  onSelect: (ids: string[], on: boolean) => void
}

/**
 * #6262 — the spools as cards: each spool's chip large, with its grams ring, so a shelf of
 * colours (and which are translucent) reads at a glance.
 */
export function SpoolCards({ groups, selected, onSelect }: Props) {
  return (
    <div className="space-y-4">
      {groups.map((group) => (
        <section key={group.key} aria-label={group.label || 'Spools'}>
          {group.label && (
            <h3 className="mb-2 text-[12px] font-medium">
              {group.label} <span className="font-normal text-muted-foreground tabular-nums">{group.spools.length}</span>
            </h3>
          )}
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(12rem,1fr))] gap-2">
            {group.spools.map((spool) => {
              const label = spool.effective?.label_weight_g
              return (
                <li
                  key={spool.id}
                  data-spool={spool.number}
                  className={cn('flex gap-3 rounded-md border p-2.5 text-[13px]', selected.has(spool.id) && 'bg-muted/60', spool.archived_at && 'opacity-70')}
                >
                  <RgbaSwatch rgba={spool.effective?.rgba ?? '#00000000'} size="lg" gramsLeft={spool.grams_left} labelWeight={label} />
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex items-center justify-between gap-2">
                      <SpoolLink spool={spool} className="font-medium underline-offset-4 hover:underline">
                        #{spool.number}
                      </SpoolLink>
                      <Checkbox
                        aria-label={`Select spool #${spool.number}`}
                        checked={selected.has(spool.id)}
                        onCheckedChange={(on) => onSelect([spool.id], on === true)}
                      />
                    </div>
                    <p className="truncate">{spool.effective?.colour_name ?? spool.names?.material ?? ''}</p>
                    <p className="truncate text-muted-foreground">{[spool.names?.material, spool.names?.brand].filter(Boolean).join(', ')}</p>
                    <p className="text-muted-foreground tabular-nums">
                      {spool.grams_left == null ? 'Weight not known' : `${grams(spool.grams_left)} g${label ? ` of ${grams(label)}` : ''}`}
                    </p>
                    <NeedsType spool={spool} />
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </div>
  )
}
