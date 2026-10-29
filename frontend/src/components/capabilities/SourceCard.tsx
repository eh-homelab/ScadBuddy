import { ChevronRight } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { aiCapabilities, type SourceView } from '../../api/capabilities'
import { SwitchField } from '../settings/form'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { matches, summarize } from './access'
import { ItemRow } from './ItemRow'

/** A source with more items than this gets a filter. */
const FILTER_ABOVE = 8

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/**
 * One source of tools (#2171): folded to its summary, open to its rows, its options
 * (`children`) and its nested sources. The switch is the agent's too (#4208).
 */
export function SourceCard({
  source,
  onChange,
  headerExtra,
  children,
}: {
  source: SourceView
  onChange: (source: SourceView) => void
  headerExtra?: ReactNode
  children?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const rows = source.items.filter((item) => matches(item, filter))

  async function setEnabled(enabled: boolean) {
    setBusy(true)
    setError(null)
    try {
      onChange(await aiCapabilities.setEnabled(source.id, enabled))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" className="px-3" role="region" aria-label={`Source ${source.title}`}>
      <Collapsible open={open} onOpenChange={setOpen}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="-ml-2 font-medium" aria-label={source.title}>
              <ChevronRight className={open ? 'rotate-90 transition-transform' : 'transition-transform'} aria-hidden />
              {source.title}
            </Button>
          </CollapsibleTrigger>
          <div className="flex items-center gap-3">
            {headerExtra}
            {source.switchable && (
              <SwitchField
                label="Enabled"
                checked={source.enabled}
                onCheckedChange={(on) => void setEnabled(on)}
                disabled={busy}
                busy={busy}
                userOnly={false}
              />
            )}
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{summarize(source)}</p>
        {error !== null && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
        <CollapsibleContent className="mt-2 flex flex-col gap-2">
          {source.items.length > FILTER_ABOVE && (
            <Input
              aria-label={`Filter ${source.title}`}
              placeholder="Filter"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="h-7 max-w-xs text-xs"
            />
          )}
          <div className="divide-y divide-border">
            {rows.map((item) => (
              <ItemRow key={item.id} source={source.id} item={item} onSaved={onChange} />
            ))}
          </div>
          {children}
          {source.children.map((child) => (
            <SourceCard key={child.id} source={child} onChange={onChange} />
          ))}
        </CollapsibleContent>
      </Collapsible>
    </Card>
  )
}
