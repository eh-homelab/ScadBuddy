import { useEffect, useState, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { Command, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { loadedSkills, matchingSkills, skillInvocation, slashQuery, type SkillChoice } from '../../agent/chat/skills'
import { aiPlugins } from '../../api/aiPlugins'
import { ApiError } from '../../api/client'

// #1920 — the composer's "/" skill menu. Typing "/" at the start of the draft lists the
// skills a session loads (agent/chat/skills.ts), filtered by what follows; the arrow keys
// move, Enter or Tab (or a click) puts `/<plugin>:<skill> ` in the draft, Escape closes it.
// Nothing is sent: the user goes on typing and sends as ever.
//
// The composer stays a textbox (a multi-line message box, not a one-line picker), with
// the listbox popup wired to it by aria-autocomplete, aria-controls and
// aria-activedescendant, so focus never leaves it; a polite status names the count, the
// option moved to and the insertion for screen readers. #3673 — the popup is shadcn's
// command list in a popover anchored to the composer; the composer keeps the keys, and
// the command list follows the highlight it is given.

interface Options {
  draft: string
  writeDraft: (text: string) => void
  focus: () => void
  /** False while the user cannot write here (another principal's session). */
  enabled: boolean
  /** The composer, which the popup sits above. */
  anchor: RefObject<HTMLElement | null>
}

interface Skills {
  list?: SkillChoice[]
  error?: string
}

export interface SkillMenu {
  /** Spread on the composer. */
  inputProps: {
    'aria-autocomplete': 'list'
    'aria-controls': string | undefined
    'aria-activedescendant': string | undefined
  }
  /** Runs first in the composer's onKeyDown; true when the menu took the key. */
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean
  /** The popup (null when closed) and its status line, rendered beside the composer. */
  menu: ReactNode
}

export function useSkillMenu({ draft, writeDraft, focus, enabled, anchor }: Options): SkillMenu {
  const query = slashQuery(draft)
  const [dismissed, setDismissed] = useState(false)
  // The highlighted option, for the query it was chosen under: a new query starts at the top.
  const [active, setActive] = useState<{ query: string | null; index: number; moved: boolean }>({
    query: null,
    index: 0,
    moved: false,
  })
  const [inserted, setInserted] = useState<string | null>(null)
  const [skills, setSkills] = useState<Skills>({})

  // A draft that no longer starts a "/" word re-arms the menu (adjusted while rendering).
  if (query === null && dismissed) setDismissed(false)
  if (query !== null && inserted !== null) setInserted(null)

  const open = enabled && query !== null && !dismissed

  // Read each time it opens, so a plugin enabled in Settings since shows up; the last
  // list stays on screen meanwhile.
  useEffect(() => {
    if (!open) return
    let live = true
    aiPlugins.listPackages().then(
      (packages) => live && setSkills({ list: loadedSkills(packages) }),
      (caught: unknown) =>
        live &&
        setSkills((s) => ({
          ...s,
          error: caught instanceof ApiError ? caught.detail : 'the assistant service did not answer.',
        })),
    )
    return () => {
      live = false
    }
  }, [open])

  const matches = open && skills.list ? matchingSkills(skills.list, query ?? '') : []
  const index = active.query === query ? Math.min(active.index, Math.max(matches.length - 1, 0)) : 0
  const moved = active.query === query && active.moved
  const highlighted = matches[index] as SkillChoice | undefined
  const current = highlighted ? index : -1

  // The command list names its own ids (cmdk), so the composer's aria-controls and
  // aria-activedescendant are read from it, and again whenever its highlight moves.
  const [list, setList] = useState<HTMLDivElement | null>(null)
  const [ids, setIds] = useState<{ list?: string; option?: string }>({})
  useEffect(() => {
    if (!list) {
      setIds((was) => (was.list || was.option ? {} : was))
      return
    }
    const read = () => {
      const option = list.querySelector('[cmdk-item][data-selected="true"]')?.id || undefined
      setIds((was) => (was.list === list.id && was.option === option ? was : { list: list.id, option }))
    }
    read()
    const observer = new MutationObserver(read)
    observer.observe(list, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-selected'] })
    return () => observer.disconnect()
  }, [list])

  // Only when the highlight moves (its option's id changes): the chat re-renders on every
  // streamed token, and scrolling then would snap back a list the user is scrolling with
  // the mouse (#2104).
  useEffect(() => {
    if (open && ids.option) document.getElementById(ids.option)?.scrollIntoView?.({ block: 'nearest' })
  }, [open, ids.option])

  const choose = (skill: SkillChoice) => {
    const text = skillInvocation(skill)
    writeDraft(text)
    setInserted(`Inserted ${text.trim()}.`)
    focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!open || event.nativeEvent.isComposing) return false
    if (event.key === 'Escape') {
      event.preventDefault()
      setDismissed(true)
      return true
    }
    const enter = event.key === 'Enter' && !event.shiftKey
    // #2104: while the list is still loading, Enter would send the half-typed "/pr" as a
    // message. Hold it until the list arrives; a failed read lets it send as ever.
    if (!skills.list && !skills.error) {
      if (enter) event.preventDefault()
      return enter
    }
    if (!highlighted) return false
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      setActive({ query, index: (index + step + matches.length) % matches.length, moved: true })
      return true
    }
    // A draft that already names the highlighted skill sends on the first Enter (#2104).
    if (enter && query === highlighted.name) return false
    if (enter || (event.key === 'Tab' && !event.shiftKey)) {
      event.preventDefault()
      choose(highlighted)
      return true
    }
    return false
  }

  let status = ''
  if (open) {
    if (!skills.list) status = skills.error ? `The skills could not be read: ${skills.error}` : 'Loading skills…'
    else if (!highlighted) status = `No skill matches “/${query}”.`
    else if (moved) status = `/${highlighted.name}, ${index + 1} of ${matches.length}`
    else
      status = `${matches.length} ${matches.length === 1 ? 'skill' : 'skills'}. Up and down arrows to choose, Enter to insert, Escape to close.`
  } else if (inserted) status = inserted

  const menu = (
    <>
      <Popover
        open={open}
        onOpenChange={(next) => {
          if (!next) setDismissed(true)
        }}
      >
        <PopoverAnchor virtualRef={anchor as RefObject<HTMLElement>} />
        <PopoverContent
          side="top"
          align="start"
          className="w-(--radix-popover-trigger-width) gap-0 p-0"
          // Focus, and the caret, stay in the composer.
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onMouseDown={(event) => event.preventDefault()}
          onInteractOutside={(event) => {
            // A click in the composer keeps it open; the draft says whether it should be.
            if (anchor.current?.contains(event.target as Node)) event.preventDefault()
          }}
        >
          <Command
            shouldFilter={false}
            value={highlighted?.name ?? ''}
            onValueChange={(name) => {
              const at = matches.findIndex((skill) => skill.name === name)
              if (at >= 0 && at !== index) setActive({ query, index: at, moved: false })
            }}
            className="rounded-lg!"
          >
            {/* Options only: the line said when there are none sits beside it, and the
                status below says it to screen readers. */}
            <CommandList ref={setList} label="Skills" className="max-h-56">
              {matches.map((skill) => (
                <CommandItem
                  key={skill.name}
                  value={skill.name}
                  onSelect={() => choose(skill)}
                  className="min-h-7 font-mono text-[13px] break-all"
                >
                  /{skill.name}
                </CommandItem>
              ))}
            </CommandList>
            {!matches.length && (
              <p data-testid="skill-menu-notice" aria-hidden="true" className="px-2.5 py-1.5 text-[13px] text-muted-foreground">
                {status}
              </p>
            )}
          </Command>
        </PopoverContent>
      </Popover>
      <p className="sr-only" role="status" aria-label="Skill suggestions">
        {status}
      </p>
    </>
  )

  return {
    inputProps: {
      'aria-autocomplete': 'list',
      'aria-controls': open ? ids.list : undefined,
      'aria-activedescendant': open && current >= 0 ? ids.option : undefined,
    },
    onKeyDown,
    menu,
  }
}
