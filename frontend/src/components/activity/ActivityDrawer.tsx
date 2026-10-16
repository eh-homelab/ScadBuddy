import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useState } from 'react'
import { ChevronRightIcon } from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { Activity, ActivityDetail } from '../../api/types'
import { formatDuration, timeAgo } from '../../lib/format'
import { useAdvanced } from '../../lib/advanced'
import { safeHttpUrl } from '../../lib/safeUrl'
import { temporalWorkflowUrl } from '../../lib/temporal'
import { ModalCompanionContext, roomFor } from '../../lib/modal'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Sheet, SheetClose, SheetContent, SheetFooter, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { principalLabel } from './labels'
import { StatusBadge } from './StatusBadge'

const ADVANCED_KEY = 'scadbuddy.activity.advanced'
const LINK = 'font-medium text-primary underline-offset-4 hover:underline'

/**
 * #798 — the room to keep at the right edge for the assistant panel (the modal
 * companion), so it stays usable beside the drawer: null with no companion, 0 when there
 * is none to spare (a phone), where the drawer covers it.
 */
function useCompanionRoom(): { companion: HTMLElement | null; room: number | null } {
  const companion = useContext(ModalCompanionContext)
  // Measured as the drawer first renders, so on a phone it mounts modal rather than
  // mounting beside and then remounting modal (Radix remounts the content when `modal` flips).
  const [room, setRoom] = useState<number | null>(() => (companion ? roomFor(companion) : null))
  useLayoutEffect(() => {
    if (!companion) return
    const measure = () => setRoom(roomFor(companion))
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    observer?.observe(companion)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      setRoom(null)
    }
  }, [companion])
  return { companion, room }
}

interface Props {
  id: string
  onClose: () => void
}

interface Stage {
  name: string
  status: string
}

/** The stages a source recorded in `detail.stages`; anything not shaped like one is left out. */
function stagesOf(detail: Record<string, unknown>): Stage[] {
  const raw = detail.stages
  if (!Array.isArray(raw)) return []
  return raw.flatMap((s: unknown) => {
    if (typeof s !== 'object' || s === null) return []
    const { name, label, status } = s as Record<string, unknown>
    const text = typeof name === 'string' ? name : typeof label === 'string' ? label : null
    return text === null ? [] : [{ name: text, status: typeof status === 'string' ? status : '' }]
  })
}

/** The error in words: a string, or an object carrying a `detail`. */
function errorText(error: unknown): string | null {
  if (typeof error === 'string') return error
  if (typeof error === 'object' && error !== null) {
    const { detail, message } = error as Record<string, unknown>
    if (typeof detail === 'string') return detail
    if (typeof message === 'string') return message
  }
  return null
}

function timing(row: Activity): string {
  if (!row.created_at) return ''
  const started = `Started ${timeAgo(row.created_at)}`
  if (!row.finished_at) return started
  return `${started}, took ${formatDuration((Date.parse(row.finished_at) - Date.parse(row.created_at)) / 1000)}`
}

/**
 * A link the backend emitted: an app path goes through the router; an `/api/` path is a
 * plain same-origin anchor (the router has no such page); an absolute URL opens in a new
 * tab only when it is http(s). Anything else is not shown.
 */
function ActivityLinkItem({ link }: { link: ActivityDetail['links'][number] }) {
  const cls = LINK
  if (link.href.startsWith('/api/')) {
    return <a href={link.href} className={cls}>{link.label}</a>
  }
  if (link.href.startsWith('/') && !link.href.startsWith('//')) {
    return <Link to={link.href} className={cls}>{link.label}</Link>
  }
  const href = safeHttpUrl(link.href)
  if (!href) return null
  return <a href={href} target="_blank" rel="noreferrer" className={cls}>{link.label}</a>
}

/**
 * Which JSON blocks are closed, per row shown, kept above the sheet: its content remounts
 * when `modal` flips.
 */
const JsonClosed = createContext<{ isOpen: (label: string) => boolean; toggle: (label: string, open: boolean) => void }>({
  isOpen: () => true,
  toggle: () => {},
})

/** A JSON block's key in the drawer's closed set: the row shown, then its label. */
function jsonKey(row: string, label: string): string {
  return `${row}\n${label}`
}

function Json({ label, value }: { label: string; value: unknown }) {
  const { isOpen, toggle } = useContext(JsonClosed)
  if (value === undefined) return null
  return (
    <Collapsible open={isOpen(label)} onOpenChange={(open) => toggle(label, open)} asChild>
      <section>
        <CollapsibleTrigger className="group/json mb-1 flex items-center gap-1 rounded-md text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          <ChevronRightIcon aria-hidden="true" className="size-3.5 transition-transform group-data-[state=open]/json:rotate-90" />
          {label}
        </CollapsibleTrigger>
        <CollapsibleContent>
          <pre className="max-h-72 overflow-auto rounded-lg border bg-ui-muted/50 p-2.5 font-mono text-xs">
            {JSON.stringify(value, null, 2)}
          </pre>
        </CollapsibleContent>
      </section>
    </Collapsible>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 text-[13px]">
      <dt className="w-24 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all">{children}</dd>
    </div>
  )
}

/** One activity row's detail, in a drawer: Simple in words, Advanced with ids and raw JSON. */
export function ActivityDrawer({ id, onClose }: Props) {
  // A child opened from the list replaces the row shown; the page keys the drawer by row.
  const [ids, setIds] = useState<string[]>([])
  const current = ids.at(-1) ?? id
  const [advanced, toggleAdvanced] = useAdvanced(ADVANCED_KEY)
  const view = useAsync(() => api.getActivity(current, advanced), [current, advanced], [`activity:${current}`])
  const settings = useAsync(() => api.getSettings(), []).data

  const row = view.data?.row
  const detail = view.data?.detail ?? {}
  const stages = stagesOf(detail)
  // Simple mode's detail has no `error` (the API drops it); the source's `summary` says it in words.
  const error = errorText(detail.error) ?? (typeof detail.summary === 'string' ? detail.summary : null)
  const temporal = row ? temporalWorkflowUrl(settings, row.workflow_id) : null

  const { companion, room } = useCompanionRoom()
  // What had focus as the drawer opened; Radix would return it to a trigger, and the
  // drawer has none (the page opens it from a row).
  const [opener] = useState(() => document.activeElement)
  // Beside the assistant panel the drawer is not modal (#798): the panel stays usable.
  const beside = companion !== null && room !== 0
  const [closed, setClosed] = useState<Set<string>>(() => new Set())
  const toggleJson = useCallback(
    (label: string, open: boolean) => {
      setClosed((was) => {
        const next = new Set(was)
        if (open) next.delete(jsonKey(current, label))
        else next.add(jsonKey(current, label))
        return next
      })
    },
    [current],
  )
  const jsonClosed = useMemo(
    () => ({ isOpen: (label: string) => !closed.has(jsonKey(current, label)), toggle: toggleJson }),
    [closed, current, toggleJson],
  )

  return (
    <JsonClosed.Provider value={jsonClosed}>
      <Sheet
        open
        modal={!beside}
        onOpenChange={(next) => {
          if (!next) onClose()
        }}
      >
        <SheetContent
          side="right"
          // Closed from the footer: the corner is where the shell raises its Assistant
          // button above an open dialog (#1897).
          showCloseButton={false}
          aria-describedby={undefined}
          // Modal to the shell (AppShell's assistant toggle reads aria-modal) unless the
          // companion beside it is usable too (#798); Radix itself sets no aria-modal.
          {...(beside ? {} : { 'aria-modal': 'true' as const })}
          // Still modal to the agent bridge (agent/dom activeDialog) either way.
          data-modal=""
          style={beside && room ? { right: room } : undefined}
          onInteractOutside={(event) => {
            // The assistant beside it; the shell's Assistant button is SheetContent's (#1897).
            if (companion?.contains(event.target as Node)) event.preventDefault()
          }}
          onOpenAutoFocus={(event) => {
            // Left in the assistant beside it: the drawer may open while the user types there.
            if (beside && companion.contains(document.activeElement)) event.preventDefault()
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            // Only focus that fell to <body> comes back: a link in the drawer may have moved it.
            const now = document.activeElement
            if ((!now || now === document.body) && opener instanceof HTMLElement && opener.isConnected) opener.focus()
          }}
          onEscapeKeyDown={(event) => {
            // The companion's own Escape (closing the assistant) is not the drawer's.
            if (beside && companion.contains(event.target as Node)) event.preventDefault()
          }}
          className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-[480px]"
        >
          <SheetHeader className="border-b sm:pr-32">
            <SheetTitle className="break-words">{row?.title ?? 'Activity'}</SheetTitle>
          </SheetHeader>
          <div className="flex items-center gap-2 border-b px-4 py-2.5">
            {ids.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => setIds(ids.slice(0, -1))}>
                Back
              </Button>
            )}
            <Label htmlFor="activity-advanced" className="ml-auto font-normal">
              Advanced
            </Label>
            <Switch id="activity-advanced" checked={advanced} onCheckedChange={toggleAdvanced} />
          </div>
          <ScrollArea className="min-h-0 flex-1">
            <div className="flex flex-col gap-4 p-4">
              {view.error ? (
                <p role="alert" className="text-[13px] text-warn">
                  Could not load this: {view.error.message}
                </p>
              ) : !row ? (
                <div role="status" aria-label="Loading" className="flex flex-col gap-2">
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-3/5" />
                </div>
              ) : (
                <>
                  <dl className="flex flex-col gap-2">
                    <Row label="Status">
                      <StatusBadge status={row.status} />
                    </Row>
                    <Row label="Started by">{principalLabel(row.principal)}</Row>
                    <Row label="When">{timing(row)}</Row>
                    {advanced && (
                      <>
                        {row.workflow_id && <Row label="Workflow">{row.workflow_id}</Row>}
                        {row.run_id && <Row label="Run">{row.run_id}</Row>}
                        {temporal && (
                          <Row label="Temporal">
                            <a href={temporal} target="_blank" rel="noreferrer" className={LINK}>
                              Temporal UI ↗
                            </a>
                          </Row>
                        )}
                        {row.traceparent && <Row label="Trace">{row.traceparent}</Row>}
                        {row.facets && Object.keys(row.facets).length > 0 && (
                          <Row label="Facets">{JSON.stringify(row.facets)}</Row>
                        )}
                        {row.via_tool && (
                          <Row label="Via tool">
                            <Link to="/settings#plugins" className={LINK}>
                              {row.via_tool}
                            </Link>
                          </Row>
                        )}
                      </>
                    )}
                  </dl>
                  {error && (
                    <p
                      className={`rounded-lg border px-3 py-2 text-[13px] ${
                        row.status === 'failed' ? 'border-destructive/30 bg-destructive/10 text-destructive' : 'text-muted-foreground'
                      }`}
                    >
                      {error}
                    </p>
                  )}
                  {stages.length > 0 && (
                    <ol aria-label="Stages" className="flex flex-col gap-1.5 text-[13px]">
                      {stages.map((s, i) => (
                        <li key={i} className="flex items-center gap-2">
                          {s.status && <StatusBadge status={s.status} />}
                          <span>{s.name}</span>
                        </li>
                      ))}
                    </ol>
                  )}
                  {(view.data?.links.length ?? 0) > 0 && (
                    <ul aria-label="Links" className="flex flex-col gap-1 text-[13px]">
                      {view.data?.links.map((l) => (
                        <li key={l.href}>
                          <ActivityLinkItem link={l} />
                        </li>
                      ))}
                    </ul>
                  )}
                  {(view.data?.children.length ?? 0) > 0 && (
                    <section>
                      <h3 className="mb-1.5 text-xs text-muted-foreground">Parts</h3>
                      <ul className="flex flex-col gap-1.5 text-[13px]">
                        {view.data?.children.map((c) => (
                          <li key={c.id} className="flex items-center gap-2">
                            <StatusBadge status={c.status} />
                            <Button
                              variant="link"
                              className="h-auto p-0 text-left whitespace-normal text-foreground"
                              onClick={() => setIds([...ids, c.id])}
                            >
                              {c.title}
                            </Button>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}
                  {advanced && (
                    <>
                      <Json label="Request" value={detail.request} />
                      <Json label="Result" value={detail.result} />
                      <Json label="Error" value={detail.error} />
                    </>
                  )}
                </>
              )}
            </div>
          </ScrollArea>
          {/* #2386 — a way out that is not Esc or the backdrop: touch has no Esc. */}
          <SheetFooter className="flex-row justify-end border-t py-3">
            <SheetClose asChild>
              <Button variant="outline">Close</Button>
            </SheetClose>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    </JsonClosed.Provider>
  )
}
