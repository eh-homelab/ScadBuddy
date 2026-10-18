import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { statusLabel } from '../../agent/chat/labels'
import { type HandoffOffer, isBusy, isOwnedByBrowser, type SessionState } from '../../agent/chat/state'
import { timeAgo } from '../../lib/format'
import { ArchiveIcon, CheckIcon, EllipsisIcon, PencilIcon, Undo2Icon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { OfferBadge, OriginBadge, OwnerBadge } from './badges'
import { usd } from './SessionBudget'

// #795 — the panel's session list: forks under their parent, each with its status,
// spend and last activity; the user's own chats can be renamed, marked done and
// archived (#1885). Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md §7, §8.

interface Props {
  /** The sessions to list, in the panel's order (newest first). */
  sessions: SessionState[]
  activeId: string | null
  onOpen: (id: string) => void
  /** Resolve once the agent took it; reject with what to show beside the row. */
  onRename: (id: string, title: string) => Promise<void>
  onDone: (id: string) => Promise<void>
  /** #1885 — puts the chat away: out of this list, read-only, until it is unarchived. */
  onArchive: (id: string) => Promise<void>
  /** #1284 — withdraws the chat's pending handoff offer. */
  onCancelOffer: (id: string) => Promise<void>
}

function outOfBudget(s: SessionState): boolean {
  return !!s.budget && s.budget.costUsd >= s.budget.budgetUsd && !isBusy(s)
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

export function SessionSwitcher({ sessions, activeId, onOpen, onRename, onDone, onArchive, onCancelOffer }: Props) {
  return (
    <ScrollArea className="[&>[data-slot=scroll-area-viewport]]:max-h-56">
    <ul className="flex flex-col gap-0.5 p-1.5">
      {grouped(sessions).map(({ root, forks }) => (
        <SessionRow
          key={root.id}
          session={root}
          activeId={activeId}
          onOpen={onOpen}
          onRename={onRename}
          onDone={onDone}
          onArchive={onArchive}
          onCancelOffer={onCancelOffer}
        >
          {forks.length > 0 && (
            <ul aria-label={`Forks of ${root.title}`} className="mt-0.5 ml-3 flex flex-col gap-0.5 border-l border-border pl-1.5">
              {forks.map((fork) => (
                <SessionRow
                  key={fork.id}
                  session={fork}
                  activeId={activeId}
                  onOpen={onOpen}
                  onRename={onRename}
                  onDone={onDone}
                  onArchive={onArchive}
                  onCancelOffer={onCancelOffer}
                />
              ))}
            </ul>
          )}
        </SessionRow>
      ))}
    </ul>
    </ScrollArea>
  )
}

/**
 * #1284 — the offer while it is live. A snapshot carries it until the agent next
 * sends one, which can be after `until` has passed; so a timer drops it once it
 * lapses, and the row renders again then. An `until` that does not parse is left
 * to the agent to judge.
 *
 * `until` is on the agent's clock and is compared with the browser's, which is
 * trusted: a browser running ahead hides the offer (and its Cancel offer) that much
 * early (#2252). Only the display is affected. The agent judges the offer on its own
 * clock (`cancelHandoff`), and a snapshot carries no server time to correct for skew.
 */
function useLiveOffer(offer: HandoffOffer | null | undefined): HandoffOffer | null {
  const until = offer ? Date.parse(offer.until) : Number.NaN
  const [lapsed, setLapsed] = useState<number | null>(null)
  useEffect(() => {
    if (!Number.isFinite(until)) return
    let timer: ReturnType<typeof setTimeout>
    // Armed again until the clock reads `until`: setTimeout's delay is a 32-bit int,
    // and a timer can run while the clock still reads short of it (it fired early, or
    // the clock stepped back), which would otherwise leave the offer showing for good.
    const arm = () => {
      const left = until - Date.now()
      if (left <= 0) setLapsed(until)
      else timer = setTimeout(arm, Math.min(left, 2 ** 31 - 1))
    }
    arm()
    return () => clearTimeout(timer)
  }, [until])
  return offer && lapsed !== until ? offer : null
}

function SessionRow({
  session: s,
  activeId,
  onOpen,
  onRename,
  onDone,
  onArchive,
  onCancelOffer,
  children,
}: Omit<Props, 'sessions'> & { session: SessionState; children?: ReactNode }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  /** The editor closed: focus goes back to the row's menu, which opened it, once that is rendered. */
  const refocus = useRef(false)
  /** Rename was picked: focus goes to the editor, not back to the menu's button. */
  const toEditor = useRef(false)
  useEffect(() => {
    if (!editing && refocus.current) {
      refocus.current = false
      menuButton.current?.focus()
    }
  }, [editing])
  const open = s.id === activeId
  const offer = useLiveOffer(s.offer)
  const mine = isOwnedByBrowser(s)
  const canFinish = mine && s.status !== 'done' && !isBusy(s)
  // The agent refuses an archive while a turn runs or anything waits on the user (409).
  const canArchive = mine && !isBusy(s)

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

  const act = async (action: (id: string) => Promise<void>) => {
    setError(null)
    try {
      await action(s.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  const label = (action: string, about: string) => (
    <span className="flex min-w-0 flex-col">
      <span>{action}</span>
      <span className="text-xs text-muted-foreground">{about}</span>
    </span>
  )

  return (
    <li>
      <div
        className={`group/row flex items-start gap-0.5 rounded-md hover:bg-muted ${open ? 'bg-muted' : ''}`}
      >
        <button
          type="button"
          aria-current={open ? 'true' : undefined}
          onClick={() => onOpen(s.id)}
          className={`flex min-w-0 flex-1 flex-col gap-1 rounded-md border-l-2 px-2.5 py-1.5 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${
            open ? 'border-primary' : 'border-transparent'
          }`}
        >
          <span className="truncate text-[13px]" title={s.title}>
            {s.title}
          </span>
          <span className="flex flex-wrap items-center gap-1.5">
            {open && <Badge className="h-[18px] px-1.5 text-[10.5px]">Open</Badge>}
            {s.parentId && <span className="text-[10.5px] text-muted-foreground">fork</span>}
            <OriginBadge origin={s.origin} />
            <OwnerBadge owner={s.owner} />
            {offer && <OfferBadge offer={offer} />}
            <span className="text-[11px] text-muted-foreground">{outOfBudget(s) ? 'Out of budget' : statusLabel(s.status)}</span>
            {s.budget && (
              <span className="text-[11px] text-muted-foreground">
                {usd(s.budget.costUsd)} of {usd(s.budget.budgetUsd)}
              </span>
            )}
            {s.updatedAt && (
              <time dateTime={s.updatedAt} className="text-[11px] text-muted-foreground">
                {timeAgo(s.updatedAt)}
              </time>
            )}
          </span>
        </button>
        {mine && !editing && (
          // #3673 — the row's actions in one menu, so a fork's row fits the panel at phone width.
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                ref={menuButton}
                variant="ghost"
                size="icon-sm"
                className="mt-1 mr-1 shrink-0"
                aria-label={`Actions for ${s.title}`}
                data-agent-user-only=""
              >
                <EllipsisIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-64"
              onCloseAutoFocus={(event) => {
                if (!toEditor.current) return
                toEditor.current = false
                event.preventDefault()
              }}
            >
              <DropdownMenuItem
                aria-label={`Rename ${s.title}`}
                data-agent-user-only=""
                onSelect={() => {
                  toEditor.current = true
                  setDraft(s.title)
                  setError(null)
                  setEditing(true)
                }}
              >
                <PencilIcon />
                Rename
              </DropdownMenuItem>
              {canFinish && (
                <DropdownMenuItem
                  aria-label={`Mark ${s.title} done`}
                  data-agent-user-only=""
                  onSelect={() => void act(onDone)}
                >
                  <CheckIcon />
                  {label('Done', 'End this chat: it takes no more messages, and Fork still continues it')}
                </DropdownMenuItem>
              )}
              {offer && (
                <DropdownMenuItem
                  aria-label={`Cancel the offer of ${s.title}`}
                  data-agent-user-only=""
                  onSelect={() => void act(onCancelOffer)}
                >
                  <Undo2Icon />
                  {label('Cancel offer', 'Withdraw the handoff offer: the chat stays yours')}
                </DropdownMenuItem>
              )}
              {canArchive && (
                <DropdownMenuItem
                  aria-label={`Archive ${s.title}`}
                  data-agent-user-only=""
                  onSelect={() => void act(onArchive)}
                >
                  <ArchiveIcon />
                  {label('Archive', 'Put this chat away: it leaves this list for Archived, read-only, until you unarchive it')}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {editing && (
        <form onSubmit={(event) => void submit(event)} className="flex items-center gap-1 px-2.5 py-1.5" data-agent-user-only="">
          <Input
            aria-label="Chat title"
            value={draft}
            maxLength={200}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onEditorKey}
            className="h-7 min-w-0 flex-1 text-[13px] md:text-[13px]"
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
        <p role="alert" className="px-2.5 pb-1.5 text-xs text-warn">
          {error}
        </p>
      )}
      {children}
    </li>
  )
}
