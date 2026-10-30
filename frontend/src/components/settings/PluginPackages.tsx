import { useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  aiPlugins,
  isBuiltIn,
  isBuiltInAnswer,
  packageFetch,
  refusalProblems,
  repinMoves,
  type FileDiff,
  type ListedPackage,
  type PackageFilesOf,
  type PackageInstall,
  type PackageReview,
  type PluginPackage,
} from '../../api/aiPlugins'
import { aiCapabilities, type SourceView } from '../../api/capabilities'
import { ApiError } from '../../api/client'
import { SourceCard } from '../capabilities/SourceCard'
import { useCapabilities } from '../capabilities/useCapabilities'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAsync } from '../../lib/useAsync'
import { ChevronRightIcon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge as UiBadge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { cn } from '@/lib/utils'
import { LoadingLines, SettingBody, SettingCard } from './form'
import { Spinner } from '../ui/spinner'
import { PackageFiles } from './PackageFiles'

/**
 * Settings → Plugin packages (#297): Claude plugins (skills, subagents, hooks,
 * `.mcp.json`) the agent service fetches from git or a marketplace entry, pinned to a
 * commit (agent/src/routes/pluginPackages.ts).
 *
 * The flow follows the server's: an install only fetches, vets and stores the pin; it
 * loads nothing until you approve exactly the commit and content hash the review shows
 * (AI design spec §8.2), and only an approved pin can be switched on. A re-pin waits,
 * with its file diff, for the same approval. A pin whose review lists refusals (a
 * command hook, a local MCP server, ...) is approved only with a second confirmation,
 * `allow_refused`, which loads it as it is.
 *
 * Each package's capabilities source (`package:<name>`, #4757) sits on its card: its switch,
 * its hooks, skills, subagents and built-ins as shared rows, and its servers with their tools.
 * ScadBuddy's own plugin is in Built-in, not here. An install that names it is answered
 * "built in", not refused.
 *
 * Every control that installs, approves, re-pins or deletes is user-only (`USER_ONLY`): the
 * in-page agent's `click` and `fill` refuse them, so an agent can never approve its own
 * plugins. The switch and each item's access are the agent's too (#4208).
 */

const isPackage = (source: SourceView) => source.kind === 'package'
const packageSource = (name: string) => `package:${name}`

/** The stored review names its items (the agent's `namesItems`); an older one's are read from its cached copy. */
const namesItems = (review: PackageReview) =>
  review.described !== undefined && review.local_servers !== undefined && review.hooks.every((h) => h.id !== undefined)

/** A server of `pkg` that a turn starts itself (an allowed package's local one). */
const localServer = (pkg: PluginPackage, server: string) => pkg.allow_refused && (pkg.review.local_servers ?? []).includes(server)

/**
 * The hook ids the current pin has, to mark a re-pin's new ones: the package source's hook items
 * and the current review's ids. `null` when they cannot be told (no source, or an older review
 * whose copy is not cached yet), so nothing is marked.
 */
function knownHooks(pkg: PluginPackage, source: SourceView | undefined): ReadonlySet<string> | null {
  if (!source) return null
  const items = source.items.filter((i) => i.kind === 'hook').map((i) => i.name)
  if (items.length === 0 && pkg.review.hooks.some((h) => h.id === undefined)) return null
  return new Set([...items, ...pkg.review.hooks.flatMap((h) => (h.id === undefined ? [] : [h.id]))])
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Links out of the Bambuddy iframe in a new tab; the sandbox allows popups (CLAUDE.md). */
function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  const safe = safeHttpUrl(href)
  if (!safe) return <span className="sb-num">{children}</span>
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" className="sb-num text-primary hover:underline">
      {children}
    </a>
  )
}

function PartList({ label, items }: { label: string; items: ReactNode[] }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5">
        {items.length === 0 ? (
          <span className="text-muted-foreground">None</span>
        ) : (
          <ul className="space-y-0.5">
            {items.map((item, i) => (
              <li key={i} className="sb-num break-all">
                {item}
              </li>
            ))}
          </ul>
        )}
      </dd>
    </div>
  )
}

/**
 * Every part of a package, as the admin reviews it before approving; `of` is whose files to read.
 * With `knownHooks` (a re-pin's review), a hook whose id the current pin lacks is marked new; a
 * hook with no id never is.
 */
export function ReviewParts({
  review,
  of,
  knownHooks = null,
}: {
  review: PackageReview
  of: PackageFilesOf
  knownHooks?: ReadonlySet<string> | null
}) {
  return (
    <dl className="grid gap-2 text-[12px] sm:grid-cols-2" aria-label={`Review of ${review.name}`}>
      <PartList label="Skills" items={review.skills.map((s) => `/${s}`)} />
      <PartList label="Commands" items={review.commands.map((c) => `/${c}`)} />
      <PartList label="Subagents" items={review.agents} />
      <PartList
        label="Hooks"
        items={review.hooks.map((h) => (
          <>
            {`${h.event}: ${h.type}${h.url ? ` → ${h.url}` : ''}`}
            {knownHooks !== null && h.id !== undefined && !knownHooks.has(h.id) && (
              <Badge tone="warn" className="ml-1.5">
                New since you last reviewed
              </Badge>
            )}
          </>
        ))}
      />
      <PartList
        label="MCP servers"
        items={review.mcp_servers.map((m) => `${m.name} (${m.type}) ${m.url}`)}
      />
      {(review.builtin_tools?.length ?? 0) > 0 && <PartList label="Built-in tools" items={review.builtin_tools!} />}
      {(review.refused?.length ?? 0) > 0 && (
        <div className="rounded-lg bg-warn/8 p-2.5 ring-1 ring-warn/40 sm:col-span-2">
          <dt className="font-medium text-warn">Refused by the vetting rules</dt>
          <dd>
            <ul className="mt-1 list-disc space-y-0.5 pl-5" aria-label={`What the rules refuse in ${review.name}`}>
              {review.refused!.map((problem) => (
                <li key={problem} className="[overflow-wrap:anywhere]">
                  {problem}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-muted-foreground">
              It loads only if you allow this when approving. Its commands, hooks and local servers then run as the
              assistant service, able to read the Claude credential, the database URL and the secrets key.
            </p>
          </dd>
        </div>
      )}
      <div className="sm:col-span-2">
        <PackageFiles review={review} of={of} />
      </div>
    </dl>
  )
}

/** Where a re-pin that moved comes from; approving it leaves the package disabled. */
function MovedNotice({ pkg }: { pkg: PluginPackage }) {
  if (!pkg.pending || !repinMoves(pkg)) return null
  const from = packageFetch(pkg)
  const place = (url: string, path: string) => (
    <span className="sb-num break-all">
      {url}
      {path && ` · ${path}`}
    </span>
  )
  return (
    <Card role="note" size="sm" className="bg-warn/8 text-xs ring-warn/40">
      <CardContent>
        This re-pin is fetched from a different place: {place(pkg.pending.plugin_url, pkg.pending.plugin_path)}, not{' '}
        {place(from.url, from.path)}. Approving it leaves the package disabled until you enable it again.
      </CardContent>
    </Card>
  )
}

function Diff({ diff }: { diff: FileDiff }) {
  const rows: [string, string, string[]][] = [
    ['+', 'Added', diff.added],
    ['−', 'Removed', diff.removed],
    ['~', 'Changed', diff.changed],
  ]
  return (
    <ul className="space-y-0.5 text-[12px]" aria-label="Files changed by the re-pin">
      {rows.flatMap(([mark, label, files]) =>
        files.map((f) => (
          <li key={`${label}:${f}`} className="sb-num break-all">
            <span aria-label={label} className="mr-1.5 inline-block w-3 text-muted-foreground">
              {mark}
            </span>
            {f}
          </li>
        )),
      )}
      {rows.every(([, , files]) => files.length === 0) && <li className="text-muted-foreground">No file changes.</li>}
    </ul>
  )
}

interface ApproveTarget {
  pkg: PluginPackage
  /** The re-pin, when approving that rather than the install. */
  pending: boolean
}

/**
 * The approval: shows the exact commit and content hash, and sends exactly those.
 * The checkbox makes the admin confirm them before the button does anything.
 */
function ApproveDialog({
  target,
  known,
  onClose,
  onApproved,
}: {
  target: ApproveTarget | null
  /** The current pin's hook ids (`knownHooks`), for a re-pin's review. */
  known: ReadonlySet<string> | null
  onClose: () => void
  onApproved: (pkg: PluginPackage) => void
}) {
  const [confirmed, setConfirmed] = useState(false)
  const [allowRefused, setAllowRefused] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!target) return null
  const { pkg, pending } = target
  const pin = pending && pkg.pending ? pkg.pending : { commit_sha: pkg.commit_sha, content_hash: pkg.content_hash, review: pkg.review }
  const refused = pin.review.refused ?? []

  const close = () => {
    setConfirmed(false)
    setAllowRefused(false)
    setError(null)
    onClose()
  }
  async function approve() {
    setBusy(true)
    setError(null)
    try {
      const saved = await aiPlugins.approvePackage(pkg.name, pin.commit_sha, pin.content_hash, refused.length > 0 && allowRefused)
      setConfirmed(false)
      setAllowRefused(false)
      onApproved(saved)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{pending ? `Approve the re-pin of ${pkg.name}` : `Approve ${pkg.name}`}</DialogTitle>
          <DialogDescription>
            Only this commit, with exactly these files, will load. Anything else needs a new approval.
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-3 text-sm">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted-foreground">Commit</dt>
            <dd className="sb-num break-all" data-testid="approve-commit">
              {pin.commit_sha}
            </dd>
            <dt className="text-muted-foreground">Content hash</dt>
            <dd className="sb-num break-all" data-testid="approve-hash">
              {pin.content_hash}
            </dd>
          </dl>
          {pending && <MovedNotice pkg={pkg} />}
          <ReviewParts
            review={pin.review}
            of={{ name: pkg.name, pending: pending && pkg.pending !== null }}
            knownHooks={pending ? known : null}
          />
          <div className="flex items-start gap-2.5" {...USER_ONLY}>
            <Checkbox
              id="approve-reviewed"
              checked={confirmed}
              onCheckedChange={(next) => setConfirmed(next === true)}
              className="mt-0.5"
            />
            <Label htmlFor="approve-reviewed" className="block leading-snug font-normal">
              I reviewed commit <span className="sb-num">{pin.commit_sha.slice(0, 12)}</span> and content hash{' '}
              <span className="sb-num">{pin.content_hash.slice(0, 19)}…</span>
            </Label>
          </div>
          {refused.length > 0 && (
            <div className="flex items-start gap-2.5" {...USER_ONLY}>
              <Checkbox
                id="approve-allow-refused"
                checked={allowRefused}
                onCheckedChange={(next) => setAllowRefused(next === true)}
                className="mt-0.5"
              />
              <Label htmlFor="approve-allow-refused" className="block leading-snug font-normal">
                Load it as it is, despite the {refused.length === 1 ? 'refusal' : `${refused.length} refusals`} above.
                Its code runs as the assistant service itself: it can read the Claude credential, the database URL
                and the key that decrypts every stored secret.
              </Label>
            </div>
          )}
          {error && (
            <p role="alert" className="text-warn">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            onClick={() => void approve()}
            disabled={!confirmed || (refused.length > 0 && !allowRefused) || busy}
            aria-busy={busy}
            {...USER_ONLY}
          >
            {busy && <Spinner />}
            Approve this pin
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function InstallForm({ onInstalled }: { onInstalled: (pkg: PluginPackage) => void }) {
  const [kind, setKind] = useState<'git' | 'marketplace'>('git')
  const [url, setUrl] = useState('')
  const [ref, setRef] = useState('')
  const [path, setPath] = useState('')
  const [entry, setEntry] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [problems, setProblems] = useState<string[]>([])
  const [notice, setNotice] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setProblems([])
    setNotice(null)
    const source: PackageInstall =
      kind === 'git'
        ? { kind, url: url.trim(), ...(ref.trim() ? { ref: ref.trim() } : {}), ...(path.trim() ? { path: path.trim() } : {}) }
        : { kind, url: url.trim(), entry: entry.trim(), ...(ref.trim() ? { ref: ref.trim() } : {}) }
    try {
      const pkg = await aiPlugins.installPackage(source)
      setUrl('')
      setRef('')
      setPath('')
      setEntry('')
      onInstalled(pkg)
    } catch (caught) {
      if (isBuiltInAnswer(caught)) {
        setNotice(message(caught))
        return
      }
      setError(message(caught))
      setProblems(refusalProblems(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-3" aria-label="Install a plugin package" {...USER_ONLY}>
      <fieldset>
        <legend className="sr-only">Source</legend>
        <RadioGroup
          name="pkg-kind"
          value={kind}
          onValueChange={(next) => setKind(next as 'git' | 'marketplace')}
          className="flex flex-wrap gap-4"
        >
          <div className="flex items-center gap-2">
            <RadioGroupItem id="pkg-kind-git" value="git" />
            <Label htmlFor="pkg-kind-git" className="font-normal">
              Git repository
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <RadioGroupItem id="pkg-kind-marketplace" value="marketplace" />
            <Label htmlFor="pkg-kind-marketplace" className="font-normal">
              Marketplace entry
            </Label>
          </div>
        </RadioGroup>
      </fieldset>
      <div>
        <Label htmlFor="pkg-url" className="mb-1.5 font-normal">
          {kind === 'git' ? 'Repository URL' : 'Marketplace repository URL'}
        </Label>
        <Input
          id="pkg-url"
          type="url"
          required
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://github.com/owner/repo.git"
          className="sb-num"
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="pkg-ref" className="mb-1.5 font-normal">
            Branch, tag or commit
          </Label>
          <Input
            id="pkg-ref"
            value={ref}
            onChange={(event) => setRef(event.target.value)}
            placeholder="HEAD"
            className="sb-num"
          />
        </div>
        {kind === 'git' ? (
          <div>
            <Label htmlFor="pkg-path" className="mb-1.5 font-normal">
              Directory in the repository
            </Label>
            <Input
              id="pkg-path"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder="the root"
              className="sb-num"
            />
          </div>
        ) : (
          <div>
            <Label htmlFor="pkg-entry" className="mb-1.5 font-normal">
              Plugin name in the marketplace
            </Label>
            <Input
              id="pkg-entry"
              required
              value={entry}
              onChange={(event) => setEntry(event.target.value)}
              className="sb-num"
            />
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={busy} aria-busy={busy} {...USER_ONLY}>
          {busy && <Spinner />}
          Fetch and review
        </Button>
        <p className="text-xs text-muted-foreground">Nothing loads until you approve the pin.</p>
      </div>
      {notice && (
        <p role="status" className="rounded-lg p-2.5 text-xs text-muted-foreground ring-1 ring-foreground/10">
          {notice}
        </p>
      )}
      {error && (
        <div role="alert" className="text-xs text-warn">
          <p>{error}</p>
          {problems.length > 0 && (
            <ul className="mt-1 list-disc space-y-0.5 pl-5" aria-label="Why the package was refused">
              {problems.map((p) => (
                <li key={p} className="[overflow-wrap:anywhere]">
                  {p}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </form>
  )
}

const TONE = {
  ok: 'border-ok/40 bg-ok/10 text-ok',
  warn: 'border-warn/40 bg-warn/10 text-warn',
  muted: 'text-muted-foreground',
} as const

function Badge({ tone, className, children }: { tone: keyof typeof TONE; className?: string; children: ReactNode }) {
  return (
    <UiBadge variant="outline" className={cn(TONE[tone], className)}>
      {children}
    </UiBadge>
  )
}

/** A card's "Review" fold: its parts, kept mounted while folded. */
function ReviewFold({ defaultOpen = false, children }: { defaultOpen?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-2">
      <CollapsibleTrigger className="inline-flex min-h-6 items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
        <ChevronRightIcon aria-hidden="true" className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
        Review
      </CollapsibleTrigger>
      <CollapsibleContent forceMount hidden={!open} className="mt-2">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}

/** A package server's "List tools": lists what the approved review showed, so it is not user-only. */
function ListTools({ source, onChange }: { source: SourceView; onChange: (source: SourceView) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function list() {
    setBusy(true)
    setError(null)
    try {
      onChange((await aiCapabilities.refresh(source.id)).source)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {error && (
        <span role="alert" className="text-xs [overflow-wrap:anywhere] text-warn">
          {error}
        </span>
      )}
      <Button
        size="sm"
        variant="outline"
        aria-label={`List tools (${source.title})`}
        onClick={() => void list()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy && <Spinner />}
        List tools
      </Button>
    </>
  )
}

function PackageCard({
  pkg,
  shadowed,
  source,
  onChange,
  onSource,
  onRemoved,
  onApprove,
}: {
  pkg: PluginPackage
  /** Stored under a built-in's name (before built-ins were listed): never loaded, only removable. */
  shadowed: boolean
  /** Its capabilities source, once read. */
  source: SourceView | undefined
  onChange: (pkg: PluginPackage) => void
  /** A source after a write: the package's or one of its servers'. */
  onSource: (source: SourceView) => void
  onRemoved: (name: string) => void
  onApprove: (target: ApproveTarget) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [repinRef, setRepinRef] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  async function act(what: string, run: () => Promise<void>) {
    setBusy(what)
    setError(null)
    try {
      await run()
    } catch (caught) {
      const problems = refusalProblems(caught)
      setError(problems.length ? `${message(caught)}: ${problems.join('; ')}` : message(caught))
    } finally {
      setBusy(null)
    }
  }

  const fetched = pkg.source
  const sourceChanged = onSource
  /**
   * The badge follows the package's switch as its source reads it, whoever wrote it (a refused
   * package's row stays on); before the source is read, the package's own `enabled`.
   */
  const enabled = source ? source.enabled || source.problem !== null : pkg.enabled
  return (
    <li aria-label={`Plugin package ${pkg.name}`}>
      <Card size="sm">
        <CardContent>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{pkg.name}</span>
            {pkg.review.version && <span className="sb-num text-xs text-muted-foreground">v{pkg.review.version}</span>}
            {enabled ? (
              <Badge tone="ok">Enabled</Badge>
            ) : pkg.approved ? (
              <Badge tone="muted">Approved, disabled</Badge>
            ) : (
              <Badge tone="warn">Awaiting approval</Badge>
            )}
            {pkg.approved && pkg.allow_refused && <Badge tone="warn">Unvetted code allowed</Badge>}
            {pkg.pending && <Badge tone="warn">Re-pin awaiting approval</Badge>}
            {shadowed && <Badge tone="warn">Not loaded</Badge>}
          </div>
          {shadowed && (
            <p role="note" className="mt-1 text-xs text-warn">
              &ldquo;{pkg.name}&rdquo; is built in, so this installed copy is never loaded. Remove it.
            </p>
          )}
          {pkg.review.description && <p className="mt-1 text-xs text-muted-foreground">{pkg.review.description}</p>}
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
            <dt className="text-muted-foreground">Source</dt>
            <dd className="break-all">
              <ExternalLink href={fetched.url}>{fetched.url}</ExternalLink>
              {fetched.kind === 'git' && fetched.path && <span className="sb-num"> · {fetched.path}</span>}
              {fetched.kind === 'marketplace' && <span className="sb-num"> · entry {fetched.entry}</span>}
              <span className="sb-num text-muted-foreground"> @ {fetched.ref}</span>
            </dd>
            <dt className="text-muted-foreground">Commit</dt>
            <dd className="sb-num break-all">{pkg.commit_sha}</dd>
            <dt className="text-muted-foreground">Content hash</dt>
            <dd className="sb-num break-all">{pkg.content_hash}</dd>
          </dl>

          <ReviewFold defaultOpen={!pkg.approved}>
            <ReviewParts review={pkg.review} of={{ name: pkg.name, pending: false }} />
          </ReviewFold>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {!pkg.approved && !shadowed && (
              <Button size="sm" onClick={() => onApprove({ pkg, pending: false })} {...USER_ONLY}>
                Approve…
              </Button>
            )}
            <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)} disabled={busy !== null} {...USER_ONLY}>
              Remove
            </Button>
          </div>

          {shadowed ? null : pkg.pending ? (
            <div className="mt-3 rounded-lg bg-primary/8 p-3 ring-1 ring-primary/40" aria-label="Pending re-pin">
              <p className="text-xs">
                Re-pin to <span className="sb-num">{pkg.pending.ref}</span> at{' '}
                <span className="sb-num break-all">{pkg.pending.commit_sha}</span>. The current pin keeps loading until you
                approve this one.
              </p>
              <div className="mt-2">
                <MovedNotice pkg={pkg} />
              </div>
              <div className="mt-2">
                <Diff diff={pkg.pending.diff} />
              </div>
              <div className="mt-2 flex gap-2">
                <Button size="sm" onClick={() => onApprove({ pkg, pending: true })} {...USER_ONLY}>
                  Approve re-pin…
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void act('discard', async () => onChange(await aiPlugins.discardRepin(pkg.name)))}
                  disabled={busy !== null}
                  {...USER_ONLY}
                >
                  Discard re-pin
                </Button>
              </div>
            </div>
          ) : (
            <form
              className="mt-3 flex items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                void act('repin', async () => onChange(await aiPlugins.repinPackage(pkg.name, repinRef.trim() || undefined)))
              }}
              {...USER_ONLY}
            >
              <div className="grow">
                <Label htmlFor={`repin-${pkg.name}`} className="mb-1.5 text-xs font-normal text-muted-foreground">
                  Re-pin to branch, tag or commit
                </Label>
                <Input
                  id={`repin-${pkg.name}`}
                  value={repinRef}
                  onChange={(event) => setRepinRef(event.target.value)}
                  placeholder={fetched.ref}
                  className="sb-num"
                />
              </div>
              <Button size="sm" variant="outline" type="submit" disabled={busy !== null} aria-busy={busy === 'repin'} {...USER_ONLY}>
                {busy === 'repin' && <Spinner />}
                Fetch re-pin
              </Button>
            </form>
          )}

          {error && (
            <p role="alert" className="mt-2 text-xs text-warn">
              {error}
            </p>
          )}

          {source && (
            <div className="mt-3">
              <SourceCard
                source={source}
                onChange={sourceChanged}
                childExtra={(child) =>
                  // An allowed package's local server is started only by a turn: its tools are never listed here.
                  localServer(pkg, child.title) ? null : <ListTools source={child} onChange={sourceChanged} />
                }
              />
              {!namesItems(pkg.review) && source.items.length === 0 && source.children.length === 0 && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Its hooks, skills, subagents and servers show here once the package is next loaded.
                </p>
              )}
            </div>
          )}

          <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>{`Remove ${pkg.name}?`}</AlertDialogTitle>
                <AlertDialogDescription>
                  Its pin is deleted and its cached files are removed. Installing it again needs a new review. Sessions
                  stop loading it from their next turn.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                {/* Not an AlertDialogAction: the dialog closes once the delete is done. */}
                <Button
                  variant="destructive"
                  disabled={busy === 'delete'}
                  aria-busy={busy === 'delete'}
                  onClick={() =>
                    void act('delete', async () => {
                      await aiPlugins.deletePackage(pkg.name)
                      setConfirmDelete(false)
                      onRemoved(pkg.name)
                    })
                  }
                  {...USER_ONLY}
                >
                  {busy === 'delete' && <Spinner />}
                  Remove package
                </Button>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>
    </li>
  )
}

export function PluginPackagesPanel() {
  const state = useAsync(() => aiPlugins.listPackages(), [])
  const caps = useCapabilities(isPackage)
  const [approve, setApprove] = useState<ApproveTarget | null>(null)
  const listed = useMemo(() => state.data ?? [], [state.data])
  // ScadBuddy's own plugin is in Built-in; a package stored under a built-in's name is still shown, to be removed.
  const packages = useMemo(() => listed.filter((p): p is PluginPackage => !isBuiltIn(p)), [listed])
  const shadowed = (name: string) => listed.some((p) => isBuiltIn(p) && p.name === name)
  const sourceOf = (name: string) => caps.sources.find((s) => s.id === packageSource(name))

  const replace = (pkg: PluginPackage) => {
    // A built-in and a package stored under its name share a name.
    const at = (p: ListedPackage) => !isBuiltIn(p) && p.name === pkg.name
    state.setData(listed.some(at) ? listed.map((p) => (at(p) ? pkg : p)) : [...listed, pkg])
  }
  /** A change to which pin is stored or approved changes its items: read the sources again. */
  const repinned = (pkg: PluginPackage) => {
    replace(pkg)
    void caps.reload()
  }

  return (
    <SettingCard title="Plugin packages" headingId="plugin-packages-heading">
      <SettingBody>
        <p className="text-xs text-muted-foreground">
          Claude plugins (skills, subagents, hooks and MCP servers) from a git repository or a marketplace, pinned to a
          commit. The assistant loads them from the next turn once approved and switched on. A plugin that runs
          commands loads only if you allow it when approving.
        </p>
        {state.loading && <LoadingLines label="Loading plugin packages" lines={['h-20 w-full', 'h-20 w-full']} />}
        {state.error && (
          <p role="alert" className="text-xs text-warn">
            Plugin packages are unavailable: {state.error.message}
          </p>
        )}
        {/* A 503 is the agent without its database: `GET /plugin-packages` says so already. */}
        {caps.error && !state.error && !(caps.error instanceof ApiError && caps.error.status === 503) && (
          <p role="alert" className="text-xs text-warn">
            Could not load the packages&rsquo; items: {caps.error.message}
          </p>
        )}
        {packages.length > 0 && (
          <ul className="space-y-3" aria-label="Installed plugin packages">
            {packages.map((pkg) => (
              <PackageCard
                key={pkg.name}
                pkg={pkg}
                shadowed={shadowed(pkg.name)}
                source={sourceOf(pkg.name)}
                onChange={replace}
                onSource={caps.replace}
                onRemoved={(name) => {
                  state.setData(listed.filter((p) => isBuiltIn(p) || p.name !== name))
                  void caps.reload()
                }}
                onApprove={setApprove}
              />
            ))}
          </ul>
        )}
        {!state.loading && !state.error && packages.length === 0 && (
          <p className="text-xs text-muted-foreground">No plugin packages installed.</p>
        )}
        <InstallForm onInstalled={repinned} />
      </SettingBody>
      <ApproveDialog
        target={approve}
        known={approve ? knownHooks(approve.pkg, sourceOf(approve.pkg.name)) : null}
        onClose={() => setApprove(null)}
        onApproved={(pkg) => {
          repinned(pkg)
          setApprove(null)
        }}
      />
    </SettingCard>
  )
}
