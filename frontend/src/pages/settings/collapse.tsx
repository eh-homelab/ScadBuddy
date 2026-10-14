import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { Chevron } from '../../components/ui/Chevron'
import { CollapseContext, foldsAround, readCollapsed, useCollapsible, useReveal, writeCollapsed } from './collapseState'

/** The Settings page's folded sections and sub-sections, remembered per browser. */
export function SettingsCollapseProvider({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const [empty, setEmptyIds] = useState<ReadonlySet<string>>(new Set())
  const setOpen = useCallback((id: string, open: boolean) => {
    setCollapsed((current) => {
      if (current.has(id) !== open) return current
      const next = new Set(current)
      if (open) next.delete(id)
      else next.add(id)
      writeCollapsed(next)
      return next
    })
  }, [])
  const setEmpty = useCallback((id: string, isEmpty: boolean) => {
    setEmptyIds((current) => {
      if (current.has(id) === isEmpty) return current
      const next = new Set(current)
      if (isEmpty) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])
  const reveal = useCallback(
    (id: string) => {
      for (const fold of foldsAround(id)) setOpen(fold, true)
    },
    [setOpen],
  )
  const value = useMemo(
    () => ({ collapsed, setOpen, empty, setEmpty, reveal }),
    [collapsed, setOpen, empty, setEmpty, reveal],
  )
  return <CollapseContext.Provider value={value}>{children}</CollapseContext.Provider>
}

/**
 * #2769 — one group of a long section (the Assistant's), with its own anchor and fold.
 * Hidden when everything in it hides itself (a setting without the agent's database), so
 * no empty heading is left, and so is its line in the section's contents.
 */
export function SubSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  const { open, toggle } = useCollapsible(id)
  const context = useContext(CollapseContext)
  const setEmpty = context?.setEmpty
  const body = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = body.current
    if (!element || !setEmpty) return
    const read = () => setEmpty(id, element.childElementCount === 0)
    read()
    const observer = new MutationObserver(read)
    observer.observe(element, { childList: true })
    return () => observer.disconnect()
  }, [id, setEmpty])
  const headingId = `${id}-heading`
  return (
    <div
      id={id}
      data-collapsible=""
      role="group"
      aria-labelledby={headingId}
      hidden={context?.empty.has(id)}
      className="scroll-mt-14 rounded-[6px] border border-line"
    >
      <h3 id={headingId} className="text-[13px] font-medium">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${id}-body`}
          onClick={toggle}
          className="flex min-h-6 w-full items-center gap-1.5 px-3 py-2 text-left"
        >
          <Chevron open={open} />
          {title}
        </button>
      </h3>
      <div id={`${id}-body`} ref={body} hidden={!open} className="space-y-4 border-t border-line p-3">
        {children}
      </div>
    </div>
  )
}

/** #2769 — the contents of a section made of `SubSection`s: a link to each one it shows. */
export function SectionContents({ label, items }: { label: string; items: readonly { id: string; title: string }[] }) {
  const context = useContext(CollapseContext)
  const reveal = useReveal()
  const navigate = useNavigate()
  const shown = items.filter((item) => !context?.empty.has(item.id))
  return (
    <nav aria-label={label}>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[12px]">
        {shown.map((item) => (
          <li key={item.id}>
            <a
              href={`#${item.id}`}
              onClick={(event) => {
                event.preventDefault()
                // Unfolded in this render, and scrolled to once it is, by the section nav,
                // which follows the address (#2345).
                reveal(item.id)
                navigate({ hash: `#${item.id}` })
              }}
              className="text-accent underline"
            >
              {item.title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}
