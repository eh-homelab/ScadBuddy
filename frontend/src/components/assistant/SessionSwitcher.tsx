import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { statusLabel } from '../../agent/chat/labels'
import { isOwnedByBrowser, type SessionState } from '../../agent/chat/state'
import { timeAgo } from '../../lib/format'
import { Button } from '../ui/Button'
import { OriginBadge, OwnerBadge } from './badges'
import { usd } from './SessionBudget'

// #795 — the panel's session list: forks under their parent, each with its status,
// spend and last activity; the user's own chats can be renamed and marked done.
// Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md §7, §8.

interface Props {
  /** The sessions to list, in the panel's order (newest first). */
  sessions: SessionState[]
  activeId: string | null
  onOpen: (id: string) => void
  /** Resolve once the agent took it; reject with what to show beside the row. */
  onRename: (id: string, title: string) => Promise<void>
  onDone: (id: string) => Promise<void>
}

const RUNNING: ReadonlySet<SessionState['status']> = new Set(['running', 'waiting_approval', 'waiting_input'])

function outOfBudget(s: SessionState): boolean {
  return !!s.budget && s.budget.costUsd >= s.budget.budgetUsd && !RUNNING.has(s.status)
}

/**
 * Each listed session whose parent is not listed, with every fork of it below (a fork
 * of a fork too): one level of indent, as the panel is narrow.
 */
function grouped(sessions: SessionState[]): { root: SessionState; forks: SessionState[] }[] {
  const listed = new Set(sessions.map((s) => s.id))
  const children = new Map<string, SessionState[]>()
  const roots: SessionState[] = []
  for (const s of sessions) {
    if (s.parentId && s.parentId !== s.id && listed.has(s.parentId)) {
      children.set(s.parentId, [...(children.get(s.parentId) ?? []), s])
    } else {
      roots.push(s)
    }
  }
  const seen = new Set<string>()
  const below = (id: string): SessionState[] =>
    (children.get(id) ?? []).flatMap((child) => {
      if (seen.has(child.id)) return []
      seen.add(child.id)
      return [child, ...below(child.id)]
    })
  return roots.map((root) => {
    seen.add(root.id)
    return { root, forks: below(root.id) }
  })
}

export function SessionSwitcher({ sessions, activeId, onOpen, onRename, onDone }: Props) {
  return (
    <ul className="max-h-56 overflow-y-auto py-1">
      {grouped(sessions).map(({ root, forks }) => (
        <SessionRow key={root.id} session={root} activeId={activeId} onOpen={onOpen} onRename={onRename} onDone={onDone}>
          {forks.length > 0 && (
            <ul aria-label={`Forks of ${root.title}`} className="ml-3 border-l border-line">
              {forks.map((fork) => (
                <SessionRow key={fork.id} session={fork} activeId={activeId} onOpen={onOpen} onRename={onRename} onDone={onDone} />
              ))}
            </ul>
          )}
        </SessionRow>
      ))}
    </ul>
  )
}

function SessionRow({
  session: s,
  activeId,
  onOpen,
  onRename,
  onDone,
  children,
}: Omit<Props, 'sessions'> & { session: SessionState; children?: ReactNode }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const renameButton = useRef<HTMLButtonElement>(null)
  /** The editor closed: focus goes back to Rename, which it replaced, once that is rendered. */
  const refocus = useRef(false)
  useEffect(() => {
    if (!editing && refocus.current) {
      refocus.current = false
      renameButton.current?.focus()
    }
  }, [editing])
  const open = s.id === activeId
  const mine = isOwnedByBrowser(s)
  const canFinish = mine && s.status !== 'done' && !RUNNING.has(s.status)

  const stopEditing = () => {
    refocus.current = true
    setEditing(false)
    setError(null)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const title = draft.trim()
    if (!title) {
      setError('A title cannot be empty.')
      return
    }
    setSaving(true)
    try {
      await onRename(s.id, title)
      stopEditing()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  const onEditorKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Escape') return
    // The editor's Escape, not the panel's (which would close it).
    event.preventDefault()
    event.stopPropagation()
    stopEditing()
  }

  const finish = async () => {
    setError(null)
    try {
      await onDone(s.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  return (
    <li>
      <div className="flex items-start">
        <button
          type="button"
          aria-current={open ? 'true' : undefined}
          onClick={() => onOpen(s.id)}
          className={`flex min-w-0 flex-1 flex-col gap-1 border-l-2 px-3 py-1.5 text-left hover:bg-surface-3 ${
            open ? 'border-accent bg-surface-3' : 'border-transparent'
          }`}
        >
          <span className="truncate text-[12.5px]">{s.title}</span>
          <span className="flex flex-wrap items-center gap-1.5">
            {open && <span className="rounded-[4px] bg-accent/15 px-1 text-[10.5px] font-medium text-accent">Open</span>}
            {s.parentId && <span className="text-[10.5px] text-faint">fork</span>}
            <OriginBadge origin={s.origin} />
            <OwnerBadge owner={s.owner} />
            <span className="text-[11px] text-faint">{outOfBudget(s) ? 'Out of budget' : statusLabel(s.status)}</span>
            {s.budget && (
              <span className="text-[11px] text-faint">
                {usd(s.budget.costUsd)} of {usd(s.budget.budgetUsd)}
              </span>
            )}
            {s.updatedAt && (
              <time dateTime={s.updatedAt} className="text-[11px] text-faint">
                {timeAgo(s.updatedAt)}
              </time>
            )}
          </span>
        </button>
        {mine && !editing && (
          <span className="flex shrink-0 items-center gap-0.5 px-1 pt-1">
            <Button
              ref={renameButton}
              variant="ghost"
              size="sm"
              aria-label={`Rename ${s.title}`}
              data-agent-user-only=""
              onClick={() => {
                setDraft(s.title)
                setError(null)
                setEditing(true)
              }}
            >
              Rename
            </Button>
            {canFinish && (
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Mark ${s.title} done`}
                title="End this chat: it takes no more messages, and Fork still continues it"
                data-agent-user-only=""
                onClick={() => void finish()}
              >
                Done
              </Button>
            )}
          </span>
        )}
      </div>
      {editing && (
        <form onSubmit={(event) => void submit(event)} className="flex items-center gap-1 px-3 pb-1.5" data-agent-user-only="">
          <input
            aria-label="Chat title"
            value={draft}
            maxLength={200}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onEditorKey}
            className="min-w-0 flex-1 rounded-[4px] border border-line bg-bg px-1.5 py-0.5 text-[12.5px] outline-none focus:border-line-strong"
          />
          <Button type="submit" size="sm" disabled={saving}>
            Save
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={stopEditing}>
            Cancel
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="px-3 pb-1.5 text-[12px] text-warn">
          {error}
        </p>
      )}
      {children}
    </li>
  )
}
