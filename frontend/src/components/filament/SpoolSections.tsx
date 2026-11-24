import type { ReactNode } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

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

