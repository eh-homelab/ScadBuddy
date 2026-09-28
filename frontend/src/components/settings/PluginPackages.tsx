import { useState, type FormEvent, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  aiPlugins,
  refusalProblems,
  type FileDiff,
  type PackageInstall,
  type PackageReview,
  type PluginPackage,
} from '../../api/aiPlugins'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

/**
 * Settings → Plugin packages (#297): Claude plugins (skills, subagents, hooks,
 * `.mcp.json`) the agent service fetches from git or a marketplace entry, pinned to a
 * commit (agent/src/routes/pluginPackages.ts).
 *
 * The flow follows the server's: an install only fetches, vets and stores the pin; it
 * loads nothing until you approve exactly the commit and content hash the review shows
 * (AI design spec §8.2), and only an approved pin can be enabled. A re-pin waits,
 * with its file diff, for the same approval.
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

/** Every part of a package, as the admin reviews it before approving. */
export function ReviewParts({ review }: { review: PackageReview }) {
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
      <div className="sm:col-span-2">
        <details>
          <summary className="cursor-pointer text-muted">Files to read ({review.files.length})</summary>
          <ul className="mt-1 space-y-0.5">
            {review.files.map((f) => (
              <li key={f} className="sb-num break-all">
                {f}
              </li>
            ))}
          </ul>
        </details>
      </div>
    </dl>
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
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!target) return null
  const { pkg, pending } = target
  const pin = pending && pkg.pending ? pkg.pending : { commit_sha: pkg.commit_sha, content_hash: pkg.content_hash, review: pkg.review }

  const close = () => {
    setConfirmed(false)
    setError(null)
    onClose()
  }
  async function approve() {
    setBusy(true)
    setError(null)
    try {
      const saved = await aiPlugins.approvePackage(pkg.name, pin.commit_sha, pin.content_hash)
      setConfirmed(false)
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
            disabled={!confirmed || busy}
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
        <ReviewParts review={pin.review} />
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

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setProblems([])
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
      {error && (
        <div role="alert" className="text-[12px] text-warn">
          <p>{error}</p>
          {problems.length > 0 && (
            <ul className="mt-1 list-disc space-y-0.5 pl-5" aria-label="Why the package was refused">
              {problems.map((p) => (
                <li key={p} className="sb-num break-all">
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

function PackageCard({
  pkg,
  onChange,
  onRemoved,
  onApprove,
}: {
  pkg: PluginPackage
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
        {pkg.pending && <Badge tone="warn">Re-pin awaiting approval</Badge>}
      </div>
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
          <ReviewParts review={pkg.review} />
        </div>
      </details>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {!pkg.approved && (
          <Button size="sm" variant="primary" onClick={() => onApprove({ pkg, pending: false })} {...USER_ONLY}>
            Approve…
          </Button>
        )}
        {pkg.approved && (
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

      {pkg.pending ? (
        <div className="mt-3 rounded-[6px] border border-accent/40 bg-accent/8 p-3" aria-label="Pending re-pin">
          <p className="text-[12px]">
            Re-pin to <span className="sb-num">{pkg.pending.ref}</span> at{' '}
            <span className="sb-num break-all">{pkg.pending.commit_sha}</span>. The current pin keeps loading until you
            approve this one.
          </p>
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
  const packages = state.data ?? []

  const replace = (pkg: PluginPackage) =>
    state.setData(
      packages.some((p) => p.name === pkg.name) ? packages.map((p) => (p.name === pkg.name ? pkg : p)) : [...packages, pkg],
    )

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface" aria-labelledby="plugin-packages-heading">
      <h2 id="plugin-packages-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Plugin packages
      </h2>
      <div className="space-y-4 p-4">
        <p className="text-[12px] text-muted">
          Claude plugins (skills, subagents, hooks and MCP servers) from a git repository or a marketplace, pinned to a
          commit. The assistant loads them from the next turn once approved and enabled. Plugins that run commands
          are refused, and a plugin&rsquo;s own tools always ask before they act.
        </p>
        {state.loading && <Spinner />}
        {state.error && (
          <p role="alert" className="text-[12px] text-warn">
            Plugin packages are unavailable: {state.error.message}
          </p>
        )}
        {packages.length > 0 && (
          <ul className="space-y-3" aria-label="Installed plugin packages">
            {packages.map((pkg) => (
              <PackageCard
                key={pkg.name}
                pkg={pkg}
                onChange={replace}
                onRemoved={(name) => state.setData(packages.filter((p) => p.name !== name))}
                onApprove={setApprove}
              />
            ))}
          </ul>
        )}
        {!state.loading && !state.error && packages.length === 0 && (
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
