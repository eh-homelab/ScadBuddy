import type { ReactNode } from 'react'
import type { SpoolEvent } from '../../api/filament'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** Spec §10's order: the page's sections and the anchor each one has. */
const SECTIONS = [
  { id: 'identity', title: 'Identity' },
  { id: 'weight', title: 'Weight' },
  { id: 'colour-and-cost', title: 'Colour and cost' },
  { id: 'printers', title: 'Printers' },
  { id: 'storage', title: 'Storage' },
  { id: 'history', title: 'History' },
] as const

/** A landmark with its heading, the target of its anchor in the index. */
export function Section({ id, title, children }: { id: (typeof SECTIONS)[number]['id']; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="scroll-mt-24 border-t pt-5 first:border-t-0 first:pt-0">
      <h2 id={`${id}-heading`} className="mb-3 text-sm font-semibold tracking-tight">
        {title}
      </h2>
      <div className="flex flex-col gap-4">{children}</div>
    </section>
  )
}

const jump = (id: string) => {
  window.history.replaceState(window.history.state, '', `#${id}`)
  document.getElementById(id)?.scrollIntoView({ block: 'start' })
}

/**
 * The section index (spec §13): at 1024 px and wider a sticky list down the left, below it
 * a select above the sections. Both are in the page; CSS shows one.
 */
export function SectionIndex() {
  return (
    <>
      <nav aria-label="Sections" className="sticky top-6 hidden self-start lg:block">
        <ul className="flex flex-col gap-0.5 border-l text-[13px]">
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                onClick={(event) => {
                  event.preventDefault()
                  jump(section.id)
                }}
                className="-ml-px block border-l border-transparent py-1 pr-2 pl-3 text-muted-foreground hover:border-foreground hover:text-foreground"
              >
                {section.title}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      <div className="lg:hidden">
        <Select value="" onValueChange={jump}>
          <SelectTrigger aria-label="Go to section" className="w-full">
            <SelectValue placeholder="Go to section" />
          </SelectTrigger>
          <SelectContent>
            {SECTIONS.map((section) => (
              <SelectItem key={section.id} value={section.id}>
                {section.title}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </>
  )
}

const KIND: Record<string, string> = {
  created: 'Added',
  edited: 'Edited',
  weighed: 'Weighed',
  dried: 'Dried',
  moved: 'Moved',
  loaded: 'Loaded into a printer',
  unloaded: 'Taken out of a printer',
  print_started: 'Print started',
  print_ended: 'Print ended',
  tag_bound: 'Tag bound',
  tag_unbound: 'Tag taken off',
  archived: 'Archived',
  restored: 'Restored',
}

const detail = (event: SpoolEvent): string => {
  const parts: string[] = []
  if (event.delta_g != null) parts.push(`${event.delta_g > 0 ? '+' : ''}${Number(event.delta_g.toFixed(1))} g`)
  else if (event.grams_after != null) parts.push(`${Number(event.grams_after.toFixed(1))} g`)
  const data = event.data as { temp_c?: number | null; hours?: string | number | null; reason?: string | null }
  if (event.kind === 'dried' && data.temp_c != null) parts.push(`${data.temp_c} °C${data.hours != null ? ` for ${data.hours} h` : ''}`)
  if (data.reason) parts.push(data.reason)
  if (event.source) parts.push(event.source.replace(/_/g, ' '))
  return parts.join(', ')
}

/** This spool's ledger, newest first. The chart and the metrics arrive with #6269. */
export function HistoryList({ events }: { events: SpoolEvent[] | null }) {
  if (events === null) return <p className="text-[13px] text-muted-foreground">Loading the history…</p>
  if (events.length === 0) return <p className="text-[13px] text-muted-foreground">Nothing has happened to this spool yet.</p>
  return (
    <ol aria-label="History of this spool" className="flex flex-col text-[13px]">
      {[...events].reverse().map((event) => (
        <li key={event.id} className="flex flex-wrap items-baseline gap-x-3 border-t py-1.5 first:border-t-0">
          <time dateTime={event.at} className="w-40 shrink-0 text-muted-foreground tabular-nums">
            {new Date(event.at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
          </time>
          <span className="font-medium">{KIND[event.kind] ?? event.kind.replace(/_/g, ' ')}</span>
          <span className="text-muted-foreground">{detail(event)}</span>
        </li>
      ))}
    </ol>
  )
}
