import type { ReactNode } from 'react'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'

/** Spec §10's order, with #6950's files and links: the page's sections and the anchor each one has. */
const SECTIONS = [
  { id: 'identity', title: 'Identity' },
  { id: 'weight', title: 'Weight' },
  { id: 'colour-and-cost', title: 'Colour and cost' },
  { id: 'printers', title: 'Printers' },
  { id: 'storage', title: 'Storage' },
  { id: 'files-and-links', title: 'Files, links and properties' },
  { id: 'history', title: 'History' },
] as const

/** Where the spool page remembers its Advanced toggle (`useAdvanced`), per viewer. */
export const SPOOL_ADVANCED_KEY = 'scadbuddy.filament.spool.advanced'

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

/** The Advanced toggle: off, the page hides its rarely used controls. */
function AdvancedToggle({ advanced, onToggle, className }: { advanced: boolean; onToggle: () => void; className?: string }) {
  return (
    <Label className={`text-[13px] font-normal text-muted-foreground hover:text-foreground ${className ?? ''}`}>
      <Switch size="sm" checked={advanced} onCheckedChange={onToggle} />
      Advanced
    </Label>
  )
}

/**
 * The section index (spec §13): at 1024 px and wider a sticky list down the left, below it
 * a select above the sections. Both are in the page; CSS shows one. The Advanced toggle
 * sits under the list, and beside the select.
 */
export function SectionIndex({ advanced, onToggleAdvanced }: { advanced: boolean; onToggleAdvanced: () => void }) {
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
        <AdvancedToggle advanced={advanced} onToggle={onToggleAdvanced} className="mt-3 pl-3" />
      </nav>
      <div data-testid="section-index-narrow" className="flex items-center gap-4 lg:hidden">
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
        <AdvancedToggle advanced={advanced} onToggle={onToggleAdvanced} className="shrink-0" />
      </div>
    </>
  )
}
