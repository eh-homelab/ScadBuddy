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
import { usePollStatus } from './useCapabilities'

/** A source with more items than this gets a filter. */
const FILTER_ABOVE = 8

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/**
 * One source of tools (#2171): folded to its summary, open to its rows, its options
 * (`children`) and its nested sources. The switch is the agent's too (#4208). A source a turn
 * does not load although it is switched on (`problem`, #4757) says why, and its switch reads on,
 * so switching it off is what it offers.
 */
export function SourceCard({
  source,
  onChange,
  headerExtra,
  childExtra,
  children,
  parentProblem = null,
  parentChecking = false,
}: {
  source: SourceView
  /** A source after a write: this card's or a nested one's, so replace it by id (`useCapabilities.replace`). */
  onChange: (source: SourceView) => void
  headerExtra?: ReactNode
  /** What each nested source's header holds besides its switch (a package server's "List tools"), at any depth. */
  childExtra?: (source: SourceView) => ReactNode
  children?: ReactNode
  /** The problem the source above already shows: not said again, and not this source's own row. */
  parentProblem?: string | null
  /** The source above is checking too, and says so itself when the page stops asking. */
  parentChecking?: boolean
}) {
  const poll = usePollStatus()
  /** Its own problem (not one it carries from the source above) leaves its own row on. */
  const ownProblem = source.problem !== null && source.problem !== parentProblem
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
                ariaLabel={`Enabled (${source.title})`}
                checked={source.enabled || ownProblem}
                onCheckedChange={(on) => void setEnabled(on)}
                disabled={busy}
                busy={busy}
                userOnly={false}
              />
            )}
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{summarize(source)}</p>
        {ownProblem && (
          <p role="note" className="text-xs [overflow-wrap:anywhere] text-warn">
            {source.problem}
          </p>
        )}
        {source.checking && !parentChecking && poll.stalled && (
          <div className="flex flex-wrap items-center gap-2">
            <p role="status" className="text-xs [overflow-wrap:anywhere] text-muted-foreground">
              {`Still checking whether "${source.title}" can load.`}
              {poll.pollError !== null && ` The last read failed: ${poll.pollError}`}
            </p>
            <Button size="sm" variant="outline" aria-label={`Retry (${source.title})`} onClick={poll.retry}>
              Retry
            </Button>
          </div>
        )}
        {error !== null && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
        {/* Kept mounted while folded, so an option's unsaved edit (the origin lists) survives a fold. */}
        <CollapsibleContent forceMount hidden={!open} className="mt-2 flex flex-col gap-2">
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
              <ItemRow key={item.id} source={source.id} item={item} onSaved={onChange} claims={source.kind !== 'builtin'} />
            ))}
          </div>
          {children}
          {source.children.map((child) => (
            <SourceCard
              key={child.id}
              source={child}
              onChange={onChange}
              headerExtra={childExtra?.(child)}
              childExtra={childExtra}
              parentProblem={source.problem}
              parentChecking={source.checking}
            />
          ))}
        </CollapsibleContent>
      </Collapsible>
    </Card>
  )
}
