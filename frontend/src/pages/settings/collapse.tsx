import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { revealSetting, useUnfolded } from './folding'

/** Whether `ref`'s element has any element children, following changes. */
function useHasChildren(ref: RefObject<HTMLElement | null>): boolean {
  const [filled, setFilled] = useState(true)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const check = () => setFilled(element.childElementCount > 0)
    check()
    const observer = new MutationObserver(check)
    observer.observe(element, { childList: true })
    return () => observer.disconnect()
  }, [ref])
  return filled
}

/**
 * #2769 — one group of the Assistant section, foldable and linked from its contents. A
 * group whose settings all hide themselves (no agent database) hides too.
 */
export function SettingsGroup({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  const [open, setOpen] = useUnfolded(id)
  const body = useRef<HTMLDivElement>(null)
  const filled = useHasChildren(body)
  return (
    <div id={id} data-foldable="" data-settings-group={title} hidden={!filled} className="scroll-mt-14">
      <h3 className="flex items-center gap-2 border-b border-line pb-1 text-[13px] font-medium">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${id}-body`}
          onClick={() => setOpen(!open)}
          className="flex flex-1 items-center justify-between gap-2 text-left"
        >
          {title}
          <span aria-hidden="true" className="text-muted">
            {open ? '▾' : '▸'}
          </span>
        </button>
      </h3>
      <div id={`${id}-body`} ref={body} hidden={!open} className="space-y-4 pt-2">
        {children}
      </div>
    </div>
  )
}

/** #2769 — links to the shown groups beside it (in its parent), in their order. */
export function GroupContents({ label }: { label: string }) {
  const [groups, setGroups] = useState<{ id: string; title: string }[]>([])
  const nav = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    const element = nav.current?.parentElement
    if (!element) return
    const read = () => {
      const next = [...element.querySelectorAll<HTMLElement>('[data-settings-group]:not([hidden])')].map((group) => ({
        id: group.id,
        title: group.dataset.settingsGroup ?? group.id,
      }))
      setGroups((current) =>
        current.length === next.length && current.every((group, i) => group.id === next[i]!.id) ? current : next,
      )
    }
    read()
    const observer = new MutationObserver(read)
    observer.observe(element, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] })
    return () => observer.disconnect()
  }, [])
  return (
    <nav ref={nav} aria-label={label} hidden={groups.length === 0}>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[12px]">
        {groups.map((group) => (
          <li key={group.id}>
            <a
              href={`#${group.id}`}
              onClick={(event) => {
                event.preventDefault()
                revealSetting(group.id, { block: 'start', behavior: 'smooth' })
              }}
              className="text-accent underline-offset-2 hover:underline"
            >
              {group.title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}
