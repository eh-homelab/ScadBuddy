import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { principalLabel } from '../agent/chat/labels'
import { useAssistantOpener } from '../agent/chat/opener'
import { api, ApiError } from '../api/client'
import type { ModelVersion, VersionDiff } from '../api/types'
import { UnifiedDiff } from '../components/UnifiedDiff'
import { useFillable } from '../agent/fillable'
import { Hint } from '../components/Hint'
import { PageLoading, PageProblem } from '../components/PageState'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge, badgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Toggle } from '@/components/ui/toggle'
import { cn } from '@/lib/utils'
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'

/** The revision's parent, which is what the diff endpoint compares against by default. */
const PARENT = ''
/** The previous revision in the Compare select: Radix refuses an item whose value is "". */
const PARENT_ITEM = 'parent'

export function VersionsPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  // #269 — live: a revision committed elsewhere (another tab, an agent) appears here.
  const modelState = useAsync(() => api.getModel(slug), [slug], [`model:${slug}`])
  const versionsState = useAsync(() => api.listVersions(slug), [slug], [`model:${slug}`])

  const [selected, setSelected] = useState<string | undefined>(undefined)
  const [base, setBase] = useState<string>(PARENT)
  const [diff, setDiff] = useState<VersionDiff | undefined>(undefined)
  const [diffError, setDiffError] = useState<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | undefined>(undefined)
  // #975 — a restore rewrites the model, so it is asked about first, and then said.
  const [confirming, setConfirming] = useState<ModelVersion | null>(null)
  const [restored, setRestored] = useState('')
  /** The revision a restore made, to take focus once the list shows it. */
  const [focusCommit, setFocusCommit] = useState<string | null>(null)

  const versions = versionsState.data
  // #184 — a built-in's history is readable, but the server refuses a restore.
  const canRestore = modelState.data?.origin === 'mine'
  const current = versions?.find((version) => version.current)

  // Open on the newest revision, and re-home the selection if the one being
  // shown disappears. It CANNOT carry a restore to the new head: a restore only
  // ever adds a commit, so the previously selected one is still in the list and
  // this never fires. `restore` moves the selection itself, off the revision it
  // is handed back.
  useEffect(() => {
    if (versions && versions.length > 0 && !versions.some((v) => v.commit === selected)) {
      setSelected(versions[0]?.commit)
      setBase(PARENT)
    }
  }, [versions, selected])

  useEffect(() => {
    if (!selected) return
    let stale = false
    setDiff(undefined)
    setDiffError(undefined)
    api
      .getVersionDiff(slug, selected, base || undefined)
      .then((next) => {
        if (!stale) setDiff(next)
      })
      .catch((cause: unknown) => {
        if (!stale) {
          setDiffError(cause instanceof ApiError ? cause.detail : 'Could not read the diff.')
        }
      })
    return () => {
      stale = true
    }
  }, [slug, selected, base])

  useEffect(() => {
    if (!focusCommit || !versions?.some((version) => version.commit === focusCommit)) return
    document.querySelector<HTMLElement>(`[data-version-select="${CSS.escape(focusCommit)}"]`)?.focus()
    setFocusCommit(null)
  }, [versions, focusCommit])

  async function restore(version: ModelVersion) {
    setBusy(true)
    setError(undefined)
    setRestored('')
    try {
      const created = await api.restoreVersion(slug, version.commit)
      // Show what just happened, not what was selected before it.
      setSelected(created.commit)
      setBase(PARENT)
      setRestored(`Restored ${version.short} as a new version, ${created.commit.slice(0, 7)}.`)
      setFocusCommit(created.commit)
      versionsState.reload()
      modelState.reload()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not restore that version.')
    } finally {
      setBusy(false)
    }
  }

  // #1445 — the assistant's fill picks what to compare with, by commit or by its label.
  const compare = useRef<HTMLButtonElement>(null)
  useFillable(compare, {
    options: () => [
      { value: PARENT, label: 'Previous revision' },
      ...(versions ?? [])
        .filter((version) => version.commit !== selected)
        .map((version) => ({ value: version.commit, label: `${version.short} — ${version.message}` })),
    ],
    value: () => base,
    set: setBase,
  })

  // The model too: whether Restore is offered depends on its origin.
  if (versionsState.loading || modelState.loading) {
    return <PageLoading label="Loading versions" />
  }

  if (versionsState.error) {
    return (
      <PageProblem title="No version history" message={versionsState.error.message}>
        <Button asChild variant="outline">
          <Link to={modelPath(slug)}>Back to the customizer</Link>
        </Button>
      </PageProblem>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-sm text-muted-foreground hover:text-foreground">
            Models
          </Link>
          <span className="text-muted-foreground">/</span>
          <Link to={modelPath(slug)} className="text-sm text-muted-foreground hover:text-foreground">
            {modelState.data?.name ?? slug}
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-sm font-medium">Versions</h1>
          {modelState.data?.origin === 'builtin' && (
            <Badge data-testid="builtin-badge" variant="outline" className="text-muted-foreground">
              Built-in template — read-only
            </Badge>
          )}
        </div>

        {modelState.error && (
          <div role="alert" className="mb-3 flex flex-col items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/10 p-3">
            <p className="text-xs text-destructive">
              Could not load this model&apos;s details: {modelState.error.message}
            </p>
            <Button type="button" variant="outline" size="sm" onClick={modelState.reload}>
              Try again
            </Button>
          </div>
        )}

        {/* Always in the page, so the message is announced when it lands. */}
        <p data-testid="versions-status" role="status" className={`text-xs text-ok ${restored ? 'mb-3' : ''}`}>
          {restored}
        </p>

        {error && (
          <p role="alert" className="mb-3 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </p>
        )}

        {versions?.length === 0 && (
          <Card className="border border-dashed text-center ring-0">
            <CardHeader>
              <CardTitle>
                <h2>No revisions yet</h2>
              </CardTitle>
              <CardDescription>Every upload, edit and restore of this model is recorded here.</CardDescription>
            </CardHeader>
          </Card>
        )}

        {versions && versions.length > 0 && (
          // #1036 — `grid-cols-1` below `lg`: the implicit `auto` track sized to the longest
          // commit message, so the list ran off a phone's screen and `truncate` never did.
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
            <ul data-testid="versions" aria-label="Revisions" className="space-y-2">
              {versions.map((version) => (
                <VersionRow
                  key={version.commit}
                  version={version}
                  selected={version.commit === selected}
                  busy={busy}
                  canRestore={canRestore}
                  onSelect={() => {
                    setSelected(version.commit)
                    setBase(PARENT)
                  }}
                  onRestore={() => setConfirming(version)}
                  onCustomize={() =>
                    void navigate(`${modelPath(slug)}?version=${encodeURIComponent(version.commit)}`)
                  }
                />
              ))}
            </ul>

            <section className="min-w-0 overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10">
              <header className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
                <h2 className="text-sm font-medium">Changes</h2>
                {/* #1036 — may shrink: an option is a whole commit message, and ran past the
                    card on a phone. */}
                <div className="ml-auto flex min-w-0 max-w-full items-center gap-2">
                  <Label htmlFor="versions-compare" className="shrink-0 text-xs font-normal text-muted-foreground">
                    Compare with
                  </Label>
                  <Select value={base === PARENT ? PARENT_ITEM : base} onValueChange={(next) => setBase(next === PARENT_ITEM ? PARENT : next)}>
                    <SelectTrigger ref={compare} id="versions-compare" size="sm" className="min-w-0 max-w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent position="popper" align="end" className="max-w-[min(32rem,calc(100vw-2rem))]">
                      <SelectItem value={PARENT_ITEM}>Previous revision</SelectItem>
                      {versions
                        .filter((version) => version.commit !== selected)
                        .map((version) => (
                          <SelectItem key={version.commit} value={version.commit}>
                            <span className="truncate">
                              {version.short} — {version.message}
                            </span>
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                </div>
              </header>

              {diffError && (
                <p role="alert" className="p-4 text-xs text-destructive">
                  {diffError}
                </p>
              )}
              {!diffError && !diff && (
                <div role="status" aria-label="Reading the diff" className="flex flex-col gap-2 p-4">
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-3/4" />
                </div>
              )}
              {!diffError && diff && <UnifiedDiff patch={diff.patch} />}
            </section>
          </div>
        )}

        <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{`Restore version ${confirming?.short ?? ''}?`}</AlertDialogTitle>
              <AlertDialogDescription>
                {current &&
                  `The model's source goes back to ${confirming?.short ?? ''}, as a new version on top of ${current.short}. Nothing is lost: ${current.short} stays in the history.`}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <p className="text-sm text-muted-foreground">{confirming?.message}</p>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <Button
                type="button"
                onClick={() => {
                  const version = confirming
                  setConfirming(null)
                  if (version) void restore(version)
                }}
              >
                Restore
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {current && (
          <p className="sb-num mt-4 text-xs text-muted-foreground">
            This model is at {current.short}.
          </p>
        )}
      </div>
    </div>
  )
}

function VersionRow({
  version,
  selected,
  busy,
  canRestore,
  onSelect,
  onRestore,
  onCustomize,
}: {
  version: ModelVersion
  selected: boolean
  busy: boolean
  canRestore: boolean
  onSelect: () => void
  onRestore: () => void
  onCustomize: () => void
}) {
  const files = version.files ?? []

  return (
    <li
      className={cn(
        'rounded-xl bg-card p-1 ring-1 transition-shadow',
        selected ? 'ring-2 ring-primary' : 'ring-foreground/10',
      )}
    >
      {/* #3672 — a toggle: pressed is the revision whose changes are shown. */}
      <Toggle
        pressed={selected}
        onPressedChange={() => onSelect()}
        data-version-select={version.commit}
        className="block h-auto w-full rounded-lg px-2 py-1.5 text-left font-normal whitespace-normal data-[state=on]:bg-transparent hover:bg-ui-muted/60"
      >
        <span className="flex items-center gap-2">
          <span className="sb-num text-xs text-primary">{version.short}</span>
          <span className="truncate text-sm">{version.message}</span>
          {version.current && (
            <Badge variant="outline" className="text-ok">
              current
            </Badge>
          )}
        </span>
        <span className="mt-1 block text-xs text-muted-foreground">
          {version.author} · {timeAgo(version.date)}
        </span>
        <span className="sb-num mt-1 block truncate text-xs text-muted-foreground">
          {files.length === 0
            ? 'no files changed'
            : files.map((file) => `${file.status} ${file.path}`).join(', ')}
        </span>
      </Toggle>

      {version.agent && <AgentAuthor agent={version.agent} short={version.short} />}

      <div className="flex flex-wrap gap-2 px-2 pt-1 pb-1.5">
        {/* #975 — named after the revision, so a list of these buttons tells them apart. */}
        <Button type="button" variant="outline" size="sm" onClick={onCustomize} aria-label={`Customize this version, ${version.short}`}>
          Customize this version
        </Button>
        {canRestore && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onRestore}
            disabled={busy || version.current}
            aria-label={`Restore this version, ${version.short}`}
          >
            Restore this version
          </Button>
        )}
      </div>
    </li>
  )
}

/**
 * #1288 — who an agent-made revision ran for, and the chat it was made in: opening it
 * shows that session in the assistant panel (`useAssistantOpener`, #931). The session is
 * read first, so one that was deleted, or that the browser user cannot see (an MCP
 * client's), says so here instead of opening the panel on nothing. Without the assistant,
 * or with no session in the trailer (an `/mcp` call), the badge is only text.
 */
function AgentAuthor({ agent, short }: { agent: NonNullable<ModelVersion['agent']>; short: string }) {
  const opener = useAssistantOpener()
  const [problem, setProblem] = useState('')
  const [opening, setOpening] = useState(false)
  // A realtime refresh can replace the row while the session is read; the chat still
  // opens, but nothing is set on a row that is gone.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const who = principalLabel(agent.principal)
  const badge = cn(badgeVariants({ variant: 'secondary' }), 'text-muted-foreground')
  const session = agent.session

  async function open(id: string) {
    if (!opener) return
    setProblem('')
    setOpening(true)
    try {
      await api.getAiSession(id)
      opener.openSession(id)
    } catch (cause) {
      if (!mounted.current) return
      setProblem(
        cause instanceof ApiError && cause.status === 404
          ? 'That chat is gone, or is not one you can open.'
          : 'Could not open that chat.',
      )
    } finally {
      if (mounted.current) setOpening(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2 px-2">
      <Hint content={agent.principal}>
        {session && opener ? (
          <button
            type="button"
            onClick={() => void open(session)}
            disabled={opening}
            aria-label={`Agent, for ${who}: open the chat that made ${short}`}
            className={cn(badge, 'hover:text-foreground')}
          >
            Agent · {who} · open chat
          </button>
        ) : (
          <span className={badge}>Agent · {who}</span>
        )}
      </Hint>
      <span role="status" className="text-[11px] text-destructive">
        {problem}
      </span>
    </div>
  )
}
