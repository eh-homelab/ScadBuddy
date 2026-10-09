import { useCallback, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  aiPlugins,
  isBuiltIn,
  isBuiltInAnswer,
  packageFetch,
  refusalProblems,
  repinMoves,
  type BuiltInPluginPackage,
  type FileDiff,
  type ListedPackage,
  type PackageFilesOf,
  type PackageInstall,
  type PackageReview,
  type PluginPackage,
} from '../../api/aiPlugins'
import { safeHttpUrl } from '../../lib/safeUrl'
import { announceHeadlessBrowser, useHeadlessBrowserChanges } from '../../lib/headlessBrowserSwitch'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'
import { PackageFiles } from './PackageFiles'

/**
 * Settings → Plugin packages (#297): Claude plugins (skills, subagents, hooks,
 * `.mcp.json`) the agent service fetches from git or a marketplace entry, pinned to a
 * commit (agent/src/routes/pluginPackages.ts).
 *
 * The flow follows the server's: an install only fetches, vets and stores the pin; it
 * loads nothing until you approve exactly the commit and content hash the review shows
 * (AI design spec §8.2), and only an approved pin can be enabled. A re-pin waits,
 * with its file diff, for the same approval. A pin whose review lists refusals (a
 * command hook, a local MCP server, ...) is approved only with a second confirmation,
 * `allow_refused`, which loads it as it is.
 *
 * The plugins that ship with the agent (ScadBuddy's own, the headless browser) are
 * listed first as "Built in": reviewable and switchable like any other, never removed
 * or re-pinned. An install that names one is answered "built in", not refused.
 *
 * Every control that installs, approves, enables, re-pins or deletes is user-only
 * (`USER_ONLY`): the in-page agent's `click` and `fill` refuse them, so an agent can
 * never approve its own plugins.
 */

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Links out of the Bambuddy iframe in a new tab; the sandbox allows popups (CLAUDE.md). */
function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  const safe = safeHttpUrl(href)
  if (!safe) return <span className="sb-num">{children}</span>
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" className="sb-num text-accent hover:underline">
      {children}
    </a>
  )
}

function PartList({ label, items }: { label: string; items: ReactNode[] }) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className="mt-0.5">
        {items.length === 0 ? (
          <span className="text-muted">None</span>
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

/** Every part of a package, as the admin reviews it before approving; `of` is whose files to read. */
export function ReviewParts({ review, of }: { review: PackageReview; of: PackageFilesOf }) {
  return (
    <dl className="grid gap-2 text-[12px] sm:grid-cols-2" aria-label={`Review of ${review.name}`}>
      <PartList label="Skills" items={review.skills.map((s) => `/${s}`)} />
      <PartList label="Commands" items={review.commands.map((c) => `/${c}`)} />
      <PartList label="Subagents" items={review.agents} />
      <PartList
        label="Hooks"
        items={review.hooks.map((h) => `${h.event}: ${h.type}${h.url ? ` → ${h.url}` : ''}`)}
      />
      <PartList
        label="MCP servers"
        items={review.mcp_servers.map((m) => `${m.name} (${m.type}) ${m.url}`)}
      />
      {(review.builtin_tools?.length ?? 0) > 0 && <PartList label="Built-in tools" items={review.builtin_tools!} />}
      {(review.refused?.length ?? 0) > 0 && (
        <div className="rounded-[6px] border border-warn/40 bg-warn/8 p-2 sm:col-span-2">
          <dt className="font-medium text-warn">Refused by the vetting rules</dt>
          <dd>
            <ul className="mt-1 list-disc space-y-0.5 pl-5" aria-label={`What the rules refuse in ${review.name}`}>
              {review.refused!.map((problem) => (
                <li key={problem} className="[overflow-wrap:anywhere]">
                  {problem}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-muted">
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
    <p role="note" className="rounded-[6px] border border-warn/40 bg-warn/8 p-2 text-[12px]">
      This re-pin is fetched from a different place: {place(pkg.pending.plugin_url, pkg.pending.plugin_path)}, not{' '}
      {place(from.url, from.path)}. Approving it leaves the package disabled until you enable it again.
    </p>
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
            <span aria-label={label} className="mr-1.5 inline-block w-3 text-muted">
              {mark}
            </span>
            {f}
          </li>
        )),
      )}
      {rows.every(([, , files]) => files.length === 0) && <li className="text-muted">No file changes.</li>}
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
  onClose,
  onApproved,
}: {
  target: ApproveTarget | null
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
    <Dialog
      open
      title={pending ? `Approve the re-pin of ${pkg.name}` : `Approve ${pkg.name}`}
      description="Only this commit, with exactly these files, will load. Anything else needs a new approval."
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void approve()}
            disabled={!confirmed || (refused.length > 0 && !allowRefused) || busy}
            aria-busy={busy}
            {...USER_ONLY}
          >
            {busy && <Spinner />}
            Approve this pin
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-[13px]">
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
          <dt className="text-muted">Commit</dt>
          <dd className="sb-num break-all" data-testid="approve-commit">
            {pin.commit_sha}
          </dd>
          <dt className="text-muted">Content hash</dt>
          <dd className="sb-num break-all" data-testid="approve-hash">
            {pin.content_hash}
          </dd>
        </dl>
        {pending && <MovedNotice pkg={pkg} />}
        <ReviewParts review={pin.review} of={{ name: pkg.name, pending: pending && pkg.pending !== null }} />
        <label className="flex items-start gap-2" {...USER_ONLY}>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            className="mt-0.5"
          />
          <span>
            I reviewed commit <span className="sb-num">{pin.commit_sha.slice(0, 12)}</span> and content hash{' '}
            <span className="sb-num">{pin.content_hash.slice(0, 19)}…</span>
          </span>
        </label>
        {refused.length > 0 && (
          <label className="flex items-start gap-2" {...USER_ONLY}>
            <input
              type="checkbox"
              checked={allowRefused}
              onChange={(event) => setAllowRefused(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              Load it as it is, despite the {refused.length === 1 ? 'refusal' : `${refused.length} refusals`} above.
              Its code runs as the assistant service itself: it can read the Claude credential, the database URL
              and the key that decrypts every stored secret.
            </span>
          </label>
        )}
        {error && (
          <p role="alert" className="text-warn">
            {error}
          </p>
        )}
      </div>
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
      <fieldset className="flex gap-4 text-[13px]">
        <legend className="sr-only">Source</legend>
        <label className="flex items-center gap-1.5">
          <input type="radio" name="pkg-kind" checked={kind === 'git'} onChange={() => setKind('git')} />
          Git repository
        </label>
        <label className="flex items-center gap-1.5">
          <input type="radio" name="pkg-kind" checked={kind === 'marketplace'} onChange={() => setKind('marketplace')} />
          Marketplace entry
        </label>
      </fieldset>
      <div>
        <label htmlFor="pkg-url" className="block text-[13px]">
          {kind === 'git' ? 'Repository URL' : 'Marketplace repository URL'}
        </label>
        <input
          id="pkg-url"
          type="url"
          required
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://github.com/owner/repo.git"
          className="sb-field sb-num mt-1.5"
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="pkg-ref" className="block text-[13px]">
            Branch, tag or commit
          </label>
          <input
            id="pkg-ref"
            value={ref}
            onChange={(event) => setRef(event.target.value)}
            placeholder="HEAD"
            className="sb-field sb-num mt-1.5"
          />
        </div>
        {kind === 'git' ? (
          <div>
            <label htmlFor="pkg-path" className="block text-[13px]">
              Directory in the repository
            </label>
            <input
              id="pkg-path"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder="the root"
              className="sb-field sb-num mt-1.5"
            />
          </div>
        ) : (
          <div>
            <label htmlFor="pkg-entry" className="block text-[13px]">
              Plugin name in the marketplace
            </label>
            <input
              id="pkg-entry"
              required
              value={entry}
              onChange={(event) => setEntry(event.target.value)}
              className="sb-field sb-num mt-1.5"
            />
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={busy} aria-busy={busy} {...USER_ONLY}>
          {busy && <Spinner />}
          Fetch and review
        </Button>
        <p className="text-[12px] text-muted">Nothing loads until you approve the pin.</p>
      </div>
      {notice && (
        <p role="status" className="rounded-[6px] border border-line p-2 text-[12px] text-muted">
          {notice}
        </p>
      )}
      {error && (
        <div role="alert" className="text-[12px] text-warn">
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

function Badge({ tone, children }: { tone: 'ok' | 'warn' | 'muted'; children: ReactNode }) {
  const cls = tone === 'ok' ? 'border-ok/40 text-ok' : tone === 'warn' ? 'border-warn/40 text-warn' : 'border-line text-muted'
  return <span className={`rounded-full border px-2 py-px text-[11px] ${cls}`}>{children}</span>
}

/** A built-in's skills and subagents on its card, not only in its review (#1297). */
function BuiltInParts({ review }: { review: PackageReview }) {
  if (review.skills.length === 0 && review.agents.length === 0) return null
  return (
    <div aria-label={`What ${review.name} adds`} className="mt-2 space-y-0.5 text-[12px]">
      {review.skills.length > 0 && (
        <p className="[overflow-wrap:anywhere]">
          <span className="text-muted">Skills: </span>
          <span className="sb-num">{review.skills.map((s) => `/${s}`).join(', ')}</span>
        </p>
      )}
      {review.agents.length > 0 && (
        <p className="[overflow-wrap:anywhere]">
          <span className="text-muted">Subagents: </span>
          <span className="sb-num">{review.agents.join(', ')}</span>
        </p>
      )}
    </div>
  )
}

/** The built-in whose switch is the headless-browser setting (agent plugins/packages/builtins.ts). */
const BROWSER_PLUGIN = 'playwright'

function BuiltInCard({ pkg, onChange }: { pkg: BuiltInPluginPackage; onChange: (pkg: ListedPackage) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      const saved = await aiPlugins.setPackageEnabled<BuiltInPluginPackage>(pkg.name, !pkg.enabled)
      onChange(saved)
      if (saved.name === BROWSER_PLUGIN) announceHeadlessBrowser(saved.enabled)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="rounded-[6px] border border-line p-3" aria-label={`Built-in plugin ${pkg.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{pkg.name}</span>
        {pkg.review.version && <span className="sb-num text-[12px] text-muted">v{pkg.review.version}</span>}
        <Badge tone="muted">Built in</Badge>
        {pkg.enabled ? <Badge tone="ok">Enabled</Badge> : <Badge tone="muted">Disabled</Badge>}
      </div>
      {pkg.review.description && <p className="mt-1 text-[12px] text-muted">{pkg.review.description}</p>}
      <BuiltInParts review={pkg.review} />
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
        <dt className="text-muted">Source</dt>
        <dd>
          Ships with ScadBuddy · <span className="sb-num break-all">{pkg.source.path}</span>
        </dd>
      </dl>
      <details className="mt-2">
        <summary className="cursor-pointer text-[12px] text-muted">Review</summary>
        <div className="mt-2">
          <ReviewParts review={pkg.review} of={{ name: pkg.name, pending: false }} />
        </div>
      </details>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={() => void toggle()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
          {busy && <Spinner />}
          {pkg.enabled ? 'Disable' : 'Enable'}
        </Button>
        <p className="text-[12px] text-muted">Built in: it cannot be removed or re-pinned.</p>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
    </li>
  )
}

function PackageCard({
  pkg,
  shadowed,
  onChange,
  onRemoved,
  onApprove,
}: {
  pkg: PluginPackage
  /** Stored under a built-in's name (before built-ins were listed): never loaded, only removable. */
  shadowed: boolean
  onChange: (pkg: PluginPackage) => void
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

  const source = pkg.source
  return (
    <li className="rounded-[6px] border border-line p-3" aria-label={`Plugin package ${pkg.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{pkg.name}</span>
        {pkg.review.version && <span className="sb-num text-[12px] text-muted">v{pkg.review.version}</span>}
        {pkg.enabled ? (
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
        <p role="note" className="mt-1 text-[12px] text-warn">
          &ldquo;{pkg.name}&rdquo; is built in, so this installed copy is never loaded. Remove it.
        </p>
      )}
      {pkg.review.description && <p className="mt-1 text-[12px] text-muted">{pkg.review.description}</p>}
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
        <dt className="text-muted">Source</dt>
        <dd className="break-all">
          <ExternalLink href={source.url}>{source.url}</ExternalLink>
          {source.kind === 'git' && source.path && <span className="sb-num"> · {source.path}</span>}
          {source.kind === 'marketplace' && <span className="sb-num"> · entry {source.entry}</span>}
          <span className="sb-num text-muted"> @ {source.ref}</span>
        </dd>
        <dt className="text-muted">Commit</dt>
        <dd className="sb-num break-all">{pkg.commit_sha}</dd>
        <dt className="text-muted">Content hash</dt>
        <dd className="sb-num break-all">{pkg.content_hash}</dd>
      </dl>

      <details className="mt-2" open={!pkg.approved}>
        <summary className="cursor-pointer text-[12px] text-muted">Review</summary>
        <div className="mt-2">
          <ReviewParts review={pkg.review} of={{ name: pkg.name, pending: false }} />
        </div>
      </details>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {!pkg.approved && !shadowed && (
          <Button size="sm" variant="primary" onClick={() => onApprove({ pkg, pending: false })} {...USER_ONLY}>
            Approve…
          </Button>
        )}
        {pkg.approved && !shadowed && (
          <Button
            size="sm"
            onClick={() =>
              void act('enable', async () => onChange(await aiPlugins.setPackageEnabled(pkg.name, !pkg.enabled)))
            }
            disabled={busy !== null}
            aria-busy={busy === 'enable'}
            {...USER_ONLY}
          >
            {busy === 'enable' && <Spinner />}
            {pkg.enabled ? 'Disable' : 'Enable'}
          </Button>
        )}
        <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)} disabled={busy !== null} {...USER_ONLY}>
          Remove
        </Button>
      </div>

      {shadowed ? null : pkg.pending ? (
        <div className="mt-3 rounded-[6px] border border-accent/40 bg-accent/8 p-3" aria-label="Pending re-pin">
          <p className="text-[12px]">
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
            <Button size="sm" variant="primary" onClick={() => onApprove({ pkg, pending: true })} {...USER_ONLY}>
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
            <label htmlFor={`repin-${pkg.name}`} className="block text-[12px] text-muted">
              Re-pin to branch, tag or commit
            </label>
            <input
              id={`repin-${pkg.name}`}
              value={repinRef}
              onChange={(event) => setRepinRef(event.target.value)}
              placeholder={source.ref}
              className="sb-field sb-num mt-1"
            />
          </div>
          <Button size="sm" type="submit" disabled={busy !== null} aria-busy={busy === 'repin'} {...USER_ONLY}>
            {busy === 'repin' && <Spinner />}
            Fetch re-pin
          </Button>
        </form>
      )}

      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}

      <Dialog
        open={confirmDelete}
        title={`Remove ${pkg.name}?`}
        description="Its pin is deleted and its cached files are removed. Installing it again needs a new review."
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() =>
                void act('delete', async () => {
                  await aiPlugins.deletePackage(pkg.name)
                  setConfirmDelete(false)
                  onRemoved(pkg.name)
                })
              }
              {...USER_ONLY}
            >
              Remove package
            </Button>
          </>
        }
      >
        <p className="text-[13px]">Sessions stop loading it from their next turn.</p>
      </Dialog>
    </li>
  )
}

export function PluginPackagesPanel() {
  const state = useAsync(() => aiPlugins.listPackages(), [])
  const [approve, setApprove] = useState<ApproveTarget | null>(null)
  const packages = useMemo(() => state.data ?? [], [state.data])

  // A built-in and a package stored under its name (before built-ins were listed) share a name.
  const same = (a: ListedPackage, b: ListedPackage) => a.name === b.name && isBuiltIn(a) === isBuiltIn(b)
  const replace = (pkg: ListedPackage) =>
    state.setData(packages.some((p) => same(p, pkg)) ? packages.map((p) => (same(p, pkg) ? pkg : p)) : [...packages, pkg])
  const { setData } = state
  useHeadlessBrowserChanges(
    useCallback(
      (enabled: boolean) =>
        setData(packages.map((p) => (isBuiltIn(p) && p.name === BROWSER_PLUGIN ? { ...p, enabled } : p))),
      [setData, packages],
    ),
  )

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface" aria-labelledby="plugin-packages-heading">
      <h2 id="plugin-packages-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Plugin packages
      </h2>
      <div className="space-y-4 p-4">
        <p className="text-[12px] text-muted">
          Claude plugins (skills, subagents, hooks and MCP servers) from a git repository or a marketplace, pinned to a
          commit. The assistant loads them from the next turn once approved and enabled. A plugin that runs commands
          loads only if you allow it when approving, and a plugin&rsquo;s own tools always ask before they act.
          ScadBuddy&rsquo;s own plugin and the headless browser are built in: switch them here like any other.
        </p>
        {state.loading && <Spinner />}
        {state.error && (
          <p role="alert" className="text-[12px] text-warn">
            Plugin packages are unavailable: {state.error.message}
          </p>
        )}
        {packages.length > 0 && (
          <ul className="space-y-3" aria-label="Installed plugin packages">
            {packages.map((pkg) =>
              isBuiltIn(pkg) ? (
                <BuiltInCard key={`built-in:${pkg.name}`} pkg={pkg} onChange={replace} />
              ) : (
                <PackageCard
                  key={pkg.name}
                  pkg={pkg}
                  shadowed={packages.some((p) => isBuiltIn(p) && p.name === pkg.name)}
                  onChange={replace}
                  onRemoved={(name) => state.setData(packages.filter((p) => isBuiltIn(p) || p.name !== name))}
                  onApprove={setApprove}
                />
              ),
            )}
          </ul>
        )}
        {!state.loading && !state.error && !packages.some((p) => !isBuiltIn(p)) && (
          <p className="text-[12px] text-muted">No plugin packages installed.</p>
        )}
        <InstallForm onInstalled={replace} />
      </div>
      <ApproveDialog
        target={approve}
        onClose={() => setApprove(null)}
        onApproved={(pkg) => {
          replace(pkg)
          setApprove(null)
        }}
      />
    </section>
  )
}
