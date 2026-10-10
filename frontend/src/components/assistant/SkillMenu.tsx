import { useEffect, useId, useState, type KeyboardEvent, type ReactNode } from 'react'
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
// option moved to and the insertion for screen readers.

interface Options {
  draft: string
  writeDraft: (text: string) => void
  focus: () => void
  /** False while the user cannot write here (another principal's session). */
  enabled: boolean
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

export function useSkillMenu({ draft, writeDraft, focus, enabled }: Options): SkillMenu {
  const id = useId()
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
  const optionId = (i: number) => `${id}-skill-${i}`
  const listId = `${id}-skills`
  const highlighted = matches[index] as SkillChoice | undefined
  const current = highlighted ? index : -1

  useEffect(() => {
    if (current < 0) return
    document.getElementById(optionId(current))?.scrollIntoView?.({ block: 'nearest' })
  })

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
    if (!highlighted) return false
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      setActive({ query, index: (index + step + matches.length) % matches.length, moved: true })
      return true
    }
    if ((event.key === 'Enter' && !event.shiftKey) || (event.key === 'Tab' && !event.shiftKey)) {
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
      {open && (
        <div className="absolute inset-x-2.5 bottom-full z-10 mb-1 max-h-56 overflow-y-auto rounded-[6px] border border-line bg-surface-2 py-1 text-[13px] shadow-lg">
          {/* Options only: the line said when there are none sits beside it, and the
              status above says it to screen readers. */}
          <div id={listId} role="listbox" aria-label="Skills">
            {matches.map((skill, i) => (
            <div
              key={skill.name}
              id={optionId(i)}
              role="option"
              aria-selected={i === index}
              // Keeps focus (and the caret) in the composer.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(skill)}
              onMouseMove={() => i !== index && setActive({ query, index: i, moved: false })}
              className={`flex min-h-7 cursor-pointer items-center break-all px-2.5 py-1 font-mono ${
                i === index ? 'bg-accent/15 text-ink' : 'text-muted'
              }`}
            >
              /{skill.name}
            </div>
            ))}
          </div>
          {!matches.length && (
            <p data-testid="skill-menu-notice" aria-hidden="true" className="px-2.5 py-1 text-faint">
              {status}
            </p>
          )}
        </div>
      )}
      <p className="sr-only" role="status" aria-label="Skill suggestions">
        {status}
      </p>
    </>
  )

  return {
    inputProps: {
      'aria-autocomplete': 'list',
      'aria-controls': open ? listId : undefined,
      'aria-activedescendant': open && current >= 0 ? optionId(current) : undefined,
    },
    onKeyDown,
    menu,
  }
}
