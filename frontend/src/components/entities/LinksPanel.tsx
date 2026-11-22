import { memo, useId, useState, type FormEvent } from 'react'
import { ArrowDownIcon, ArrowUpIcon, ExternalLinkIcon, PencilIcon, Trash2Icon } from 'lucide-react'
import { NEW_TAB } from '../../lib/embed'
import { failure } from '../../lib/mediaFiles'
import { useAsync } from '../../lib/useAsync'
import { entityApi, type EntityLink } from './api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'

/** Spec 6.3's known link kinds; `kind` is open text, so another is shown as it is. */
const LINK_KIND_LABELS: Record<string, string> = {
  support: 'Support',
  wiki: 'Wiki',
  manual: 'Manual',
  video: 'Video',
  datasheet: 'Datasheet',
  firmware: 'Firmware',
  purchase: 'Purchase',
  other: 'Other',
}
const KINDS = Object.keys(LINK_KIND_LABELS)

const kindLabel = (kind: string) => LINK_KIND_LABELS[kind] ?? kind

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** Only an `http` or `https` URL becomes an anchor; anything else is shown as text. */
function isWebUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url)
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
}

/** What a link is called on the page: its title, else its host. */
const nameOf = (link: EntityLink) => link.title || hostOf(link.url)

/** The links in kind groups, each group where its first link stands. */
function grouped(links: EntityLink[]): [string, EntityLink[]][] {
  const groups = new Map<string, EntityLink[]>()
  for (const link of links) groups.set(link.kind, [...(groups.get(link.kind) ?? []), link])
  return [...groups]
}

type Draft = { url: string; kind: string; title: string; note: string }
const EMPTY: Draft = { url: '', kind: 'other', title: '', note: '' }

/**
 * #6950 — the web pages about any entity (`entity` is `kind:ref`): a support page, a
 * datasheet, a store listing. Grouped by kind, in the order the server keeps; each one opens
 * in a new tab without the opener. The server decides what a link may be (`bad_link_url`).
 */
/** Memoized: the forms it sits in re-render on every keystroke, and its props are strings. */
export const LinksPanel = memo(function LinksPanel({ entity, title = 'Links' }: { entity: string; title?: string }) {
  const list = useAsync(() => entityApi.links(entity), [entity])
  const links = list.data ?? []
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function add(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await entityApi.addLink(entity, { url: draft.url.trim(), kind: draft.kind, title: draft.title.trim(), note: draft.note.trim() })
      setDraft(EMPTY)
      list.refresh()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <h3 className="text-[13px] font-medium">{title}</h3>
      {list.error ? (
        <p role="alert" className="text-[13px] text-warn">
          The links could not be loaded: {failure(list.error)}
        </p>
      ) : list.loading && !list.data ? (
        <p className="text-[13px] text-muted-foreground">Loading the links…</p>
      ) : links.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No links yet.</p>
      ) : (
        <div className="flex flex-col gap-3">
          {grouped(links).map(([kind, group]) => (
            <LinkGroup key={kind} entity={entity} kind={kind} links={group} onChanged={() => list.refresh()} />
          ))}
        </div>
      )}

      <form noValidate onSubmit={(event) => void add(event)} aria-label="Add a link" className="flex flex-col gap-2 rounded-md border border-dashed p-2.5">
        <div className="flex flex-wrap gap-2">
          <Input
            value={draft.url}
            onChange={(event) => setDraft({ ...draft, url: event.target.value })}
            aria-label="Link address"
            inputMode="url"
            placeholder="https://…"
            className="h-8 min-w-0 flex-[2_1_14rem]"
          />
          <Select value={draft.kind} onValueChange={(kind) => setDraft({ ...draft, kind })}>
            <SelectTrigger size="sm" aria-label="Link kind" className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper">
              {KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {kindLabel(kind)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            value={draft.title}
            onChange={(event) => setDraft({ ...draft, title: event.target.value })}
            aria-label="Link title"
            placeholder="Title (optional)"
            maxLength={200}
            className="h-8 min-w-0 flex-[1_1_10rem]"
          />
          <Button type="submit" size="sm" disabled={busy || draft.url.trim() === ''}>
            {busy && <Spinner />}
            Add link
          </Button>
        </div>
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
      </form>
    </section>
  )
})

function LinkGroup({ entity, kind, links, onChanged }: { entity: string; kind: string; links: EntityLink[]; onChanged: () => void }) {
  const heading = useId()
  return (
    <div className="flex flex-col gap-1">
      <h4 id={heading} className="text-[12px] text-muted-foreground">
        {kindLabel(kind)}
      </h4>
      <ul aria-labelledby={heading} className="flex flex-col divide-y rounded-md border">
        {links.map((link, at) => (
          <LinkRow key={link.id} entity={entity} link={link} before={links[at - 1]} after={links[at + 1]} onChanged={onChanged} />
        ))}
      </ul>
    </div>
  )
}

function LinkRow({
  entity,
  link,
  before,
  after,
  onChanged,
}: {
  entity: string
  link: EntityLink
  /** The link above it in its group, whose place a move up takes. */
  before?: EntityLink
  after?: EntityLink
  onChanged: () => void
}) {
  const name = nameOf(link)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Draft>({ url: link.url, kind: link.kind, title: link.title, note: link.note })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const kinds = KINDS.includes(link.kind) ? KINDS : [link.kind, ...KINDS]

  async function act(call: () => Promise<unknown>, done?: () => void) {
    setBusy(true)
    setError(null)
    try {
      await call()
      done?.()
      onChanged()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  // A move takes the neighbour's position; the server moves the rest down.
  const moveUp = () => before && act(() => entityApi.updateLink(entity, link.id, { position: before.position }))
  const moveDown = () => after && act(() => entityApi.updateLink(entity, after.id, { position: link.position }))

  if (editing) {
    return (
      <li className="p-2">
        <form
          noValidate
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void act(
              () => entityApi.updateLink(entity, link.id, { url: draft.url.trim(), kind: draft.kind, title: draft.title.trim(), note: draft.note.trim() }),
              () => setEditing(false),
            )
          }}
        >
          <div className="flex flex-wrap gap-2">
            <Input value={draft.url} onChange={(event) => setDraft({ ...draft, url: event.target.value })} aria-label={`Address of ${name}`} inputMode="url" className="h-8 min-w-0 flex-[2_1_14rem]" />
            <Select value={draft.kind} onValueChange={(kind) => setDraft({ ...draft, kind })}>
              <SelectTrigger size="sm" aria-label={`Kind of ${name}`} className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {kinds.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {kindLabel(kind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} aria-label={`Title of ${name}`} placeholder="Title" maxLength={200} className="h-8 min-w-0 flex-[1_1_10rem]" />
            <Input value={draft.note} onChange={(event) => setDraft({ ...draft, note: event.target.value })} aria-label={`Note on ${name}`} placeholder="Note" maxLength={1000} className="h-8 min-w-0 flex-[2_1_14rem]" />
          </div>
          <div className="flex gap-1">
            <Button size="xs" type="submit" disabled={busy}>
              {busy && <Spinner />}
              Save
            </Button>
            <Button
              size="xs"
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setEditing(false)
                setDraft({ url: link.url, kind: link.kind, title: link.title, note: link.note })
                setError(null)
              }}
            >
              Cancel
            </Button>
          </div>
          {error && (
            <p role="alert" className="text-xs text-warn">
              {error}
            </p>
          )}
        </form>
      </li>
    )
  }

  return (
    <li className="flex flex-wrap items-start gap-x-2 gap-y-1 px-2.5 py-1.5 text-[13px]">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-baseline gap-2">
          {isWebUrl(link.url) ? (
            <a href={link.url} {...NEW_TAB} className="truncate font-medium underline-offset-4 hover:underline">
              {name}
            </a>
          ) : (
            <span className="truncate font-medium">{name}</span>
          )}
          {link.title && <span className="truncate text-[12px] text-muted-foreground">{hostOf(link.url)}</span>}
          {isWebUrl(link.url) && <ExternalLinkIcon aria-hidden="true" className="size-3 shrink-0 self-center text-muted-foreground" />}
        </span>
        {link.note && <span className="text-[12px] text-muted-foreground">{link.note}</span>}
        {error && (
          <span role="alert" className="text-xs text-warn">
            {error}
          </span>
        )}
      </div>
      <div className="flex items-center gap-0.5">
        <Button variant="ghost" size="icon-sm" aria-label={`Move ${name} up`} title="Move up" disabled={busy || !before} onClick={() => void moveUp()}>
          <ArrowUpIcon aria-hidden="true" />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label={`Move ${name} down`} title="Move down" disabled={busy || !after} onClick={() => void moveDown()}>
          <ArrowDownIcon aria-hidden="true" />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label={`Edit ${name}`} title="Edit"
          disabled={busy}
          onClick={() => {
            setDraft({ url: link.url, kind: link.kind, title: link.title, note: link.note })
            setEditing(true)
          }}
        >
          <PencilIcon aria-hidden="true" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Remove ${name}`}
          title="Remove"
          disabled={busy}
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          onClick={() => void act(() => entityApi.deleteLink(entity, link.id))}
        >
          {busy ? <Spinner /> : <Trash2Icon aria-hidden="true" />}
        </Button>
      </div>
    </li>
  )
}
