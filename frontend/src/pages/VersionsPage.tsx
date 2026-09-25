import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { ModelVersion, VersionDiff } from '../api/types'
import { UnifiedDiff } from '../components/UnifiedDiff'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'

/** The revision's parent, which is what the diff endpoint compares against by default. */
const PARENT = ''

export function VersionsPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const modelState = useAsync(() => api.getModel(slug), [slug])
  const versionsState = useAsync(() => api.listVersions(slug), [slug])

  const [selected, setSelected] = useState<string | undefined>(undefined)
  const [base, setBase] = useState<string>(PARENT)
  const [diff, setDiff] = useState<VersionDiff | undefined>(undefined)
  const [diffError, setDiffError] = useState<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | undefined>(undefined)

  const versions = versionsState.data
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

  async function restore(commit: string) {
    setBusy(true)
    setError(undefined)
    try {
      const created = await api.restoreVersion(slug, commit)
      // Show what just happened, not what was selected before it.
      setSelected(created.commit)
      setBase(PARENT)
      versionsState.reload()
      modelState.reload()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not restore that version.')
    } finally {
      setBusy(false)
    }
  }

  if (versionsState.loading) {
    return (
      <p className="flex items-center gap-2 px-4 py-16 text-[13px] text-muted">
        <Spinner /> Loading versions
      </p>
    )
  }

  if (versionsState.error) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">No version history</h1>
        <p className="mt-2 text-[13px] text-muted">{versionsState.error.message}</p>
        <Link to={`/m/${slug}`} className="mt-4 inline-block text-[13px] text-accent underline">
          Back to the customizer
        </Link>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={`/m/${slug}`} className="text-[12px] text-muted hover:text-ink">
            {modelState.data?.name ?? slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">Versions</h1>
        </div>

        {error && (
          <p role="alert" className="mb-3 rounded-[6px] bg-warn/10 px-3 py-2 text-[12px] text-warn">
            {error}
          </p>
        )}

        {versions?.length === 0 && (
          <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
            <h2 className="text-[15px] font-medium">No revisions yet</h2>
            <p className="mt-2 text-[13px] text-muted">
              Every upload, edit and restore of this model is recorded here.
            </p>
          </div>
        )}

        {versions && versions.length > 0 && (
          <div className="grid gap-4 lg:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
            <ul data-testid="versions" aria-label="Revisions" className="space-y-2">
              {versions.map((version) => (
                <VersionRow
                  key={version.commit}
                  version={version}
                  selected={version.commit === selected}
                  busy={busy}
                  onSelect={() => {
                    setSelected(version.commit)
                    setBase(PARENT)
                  }}
                  onRestore={() => void restore(version.commit)}
                  onCustomize={() =>
                    void navigate(`/m/${slug}?version=${encodeURIComponent(version.commit)}`)
                  }
                />
              ))}
            </ul>

            <section className="min-w-0 rounded-[6px] border border-line bg-surface">
              <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
                <h2 className="text-[12px] font-medium">Changes</h2>
                <label className="ml-auto flex items-center gap-2 text-[12px] text-muted">
                  Compare with
                  <select
                    value={base}
                    onChange={(event) => setBase(event.target.value)}
                    className="rounded-[6px] border border-line bg-surface-2 px-2 py-1 text-[12px] text-ink"
                  >
                    <option value={PARENT}>Previous revision</option>
                    {versions
                      .filter((version) => version.commit !== selected)
                      .map((version) => (
                        <option key={version.commit} value={version.commit}>
                          {version.short} — {version.message}
                        </option>
                      ))}
                  </select>
                </label>
              </header>

              {diffError && (
                <p role="alert" className="p-4 text-[12px] text-warn">
                  {diffError}
                </p>
              )}
              {!diffError && !diff && (
                <p className="flex items-center gap-2 p-4 text-[12px] text-muted">
                  <Spinner /> Reading the diff
                </p>
              )}
              {!diffError && diff && <UnifiedDiff patch={diff.patch} />}
            </section>
          </div>
        )}

        {current && (
          <p className="sb-num mt-4 text-[12px] text-faint">
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
  onSelect,
  onRestore,
  onCustomize,
}: {
  version: ModelVersion
  selected: boolean
  busy: boolean
  onSelect: () => void
  onRestore: () => void
  onCustomize: () => void
}) {
  const files = version.files ?? []

  return (
    <li
      className={`rounded-[6px] border bg-surface p-3 ${
        selected ? 'border-accent' : 'border-line'
      }`}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className="block w-full text-left"
      >
        <span className="flex items-center gap-2">
          <span className="sb-num text-[12px] text-accent">{version.short}</span>
          <span className="truncate text-[13px] text-ink">{version.message}</span>
          {version.current && <span className="text-[11px] text-ok">current</span>}
        </span>
        <span className="mt-1 block text-[12px] text-muted">
          {version.author} · {timeAgo(version.date)}
        </span>
        <span className="sb-num mt-1 block truncate text-[12px] text-faint">
          {files.length === 0
            ? 'no files changed'
            : files.map((file) => `${file.status} ${file.path}`).join(', ')}
        </span>
      </button>

      <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" onClick={onCustomize}>
          Customize this version
        </Button>
        <Button size="sm" onClick={onRestore} disabled={busy || version.current}>
          Restore this version
        </Button>
      </div>
    </li>
  )
}
