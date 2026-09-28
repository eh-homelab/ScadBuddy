import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { api } from '../api/client'
import { DuplicateModelButton } from '../components/DuplicateModelButton'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { modelPath } from '../lib/deeplink'
import { countConflicts } from '../lib/upstream'
import { useAsync } from '../lib/useAsync'

/**
 * The same editor as "New model", prefilled. Saving overwrites the source in place.
 * A built-in template (#184) opens read-only: the server refuses to write it, and
 * "Duplicate to edit" (#159) opens an editable copy instead.
 *
 * `?merge` (#160) opens a duplicate on its conflicted upstream merge instead: the
 * marked-up source to resolve, saved with the upstream revision it resolves as
 * `merge_base`.
 */
export function EditSourcePage() {
  const { slug = '' } = useParams()
  const merging = useSearchParams()[0].has('merge')
  const loaded = useAsync(() => api.getSource(slug), [slug])
  const model = useAsync(() => api.getModel(slug), [slug])
  const upstream = useAsync(
    async () => (merging ? await api.getUpstream(slug) : null),
    [slug, merging],
  )
  const builtin = model.data?.origin === 'builtin'
  // The preview is worked out afresh, so the source and the revision it merges agree.
  const merge =
    // #235: an update or a dismissed one, which still merges. The backend sets
    // `preview` exactly when the state is 'update' or 'dismissed' (see
    // Catalogue.upstream_status), so checking `preview` stands in for the state.
    upstream.data?.preview && upstream.data.revision
      ? {
          merged: upstream.data.preview.merged,
          base: upstream.data.revision,
          id: upstream.data.upstream.id,
        }
      : null
  const [source, setSource] = useState<string | null>(null)
  const navigate = useNavigate()

  const initial = merge ? merge.merged : upstream.loading ? undefined : loaded.data
  useEffect(() => {
    if (initial !== undefined) setSource(initial)
  }, [initial])

  async function save(force: boolean) {
    if (merge) await api.resolveUpstreamMerge(slug, source ?? '', merge.base, force)
    else await api.replaceSource(slug, source ?? '', force)
    await navigate(modelPath(slug))
  }

  if (loaded.loading || model.loading || upstream.loading || (source === null && !loaded.error)) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading the source
      </p>
    )
  }

  if (loaded.error || source === null) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">That source is not here</h1>
        <p className="mt-2 text-[13px] text-muted">{loaded.error?.message}</p>
        <Link to="/" className="mt-4 inline-block text-[13px] text-accent underline">
          Back to models
        </Link>
      </div>
    )
  }

  // Whether the source may be saved is the record's to say; without it, nothing is offered.
  if (model.error) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">Could not load this model</h1>
        <p className="mt-2 text-[13px] text-muted">{model.error.message}</p>
        <Button size="sm" className="mt-4" onClick={model.reload}>
          Try again
        </Button>
      </div>
    )
  }

  const conflicts = countConflicts(source)

  return (
    <SourceWorkbench
      breadcrumb={
        <>
          <Link to={modelPath(slug)} className="shrink-0 text-[12px] text-muted hover:text-ink">
            {slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="truncate text-[13px] font-medium">
            {builtin ? 'View source' : merge ? 'Resolve update' : 'Edit source'}
          </h1>
        </>
      }
      fields={
        merging && (
          <p
            data-testid="merge-banner"
            role="status"
            className={`border-t border-line px-3 py-1.5 text-[12px] ${merge ? 'text-muted' : 'text-warn'}`}
          >
            {merge
              ? `Resolving the update from ${merge.id}. Edit each conflict, from <<<<<<< to >>>>>>>, ` +
                `down to the lines to keep, then save. ` +
                (conflicts === 0
                  ? 'No conflicts left.'
                  : `${conflicts === 1 ? '1 conflict' : `${conflicts} conflicts`} left.`)
              : upstream.error
                ? `Could not load the update to resolve: ${upstream.error.message}`
                : // Taken or dismissed since: this is the source as it stands.
                  'There is no update to resolve any more; this is the source as it is.'}
          </p>
        )
      }
      uri={`file:///models/${slug}/model.scad`}
      slug={slug}
      source={source}
      onSourceChange={setSource}
      saveLabel={merge ? 'Save resolution' : 'Save source'}
      canSave={model.data?.origin === 'mine'}
      readOnly={builtin}
      readOnlyActions={
        // #159 — the way to edit a built-in: a copy of it, opened on its source.
        <DuplicateModelButton
          slug={slug}
          name={model.data?.name ?? slug}
          label="Duplicate to edit"
          landOn="source"
          primary
        />
      }
      onSave={save}
    />
  )
}
