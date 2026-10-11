import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router'
import { ApiError, api } from '../api/client'
import { SourceFileTabs } from '../components/SourceFileTabs'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { Spinner } from '../components/ui/Spinner'
import { modelPath, sourceFilePath } from '../lib/deeplink'
import { MAIN_SOURCE, SOURCE_FILE_PATTERN } from '../lib/sourceFiles'
import { useAsync } from '../lib/useAsync'

/**
 * #1290 — one of a model's other `.scad` files, the ones `model.scad` can `include` or
 * `use`: read with `GET /models/{slug}/files/{name}`, saved with `PUT` as one revision,
 * and deleted with `DELETE`. `model.scad` itself is the Edit source page.
 *
 * A save carries `base`, the model's revision the file was read at, so a change made
 * since (another tab, the agent, a save of `model.scad`) is a 409 that writes nothing.
 * The page then offers to reload the file or to save over it, as Edit source does.
 */
export function EditSourceFilePage() {
  const { slug = '', file = '' } = useParams()
  // One editor per file: switching files mounts a fresh one, so nothing from the last
  // file (a "changed elsewhere" banner and the base Keep editing would save against,
  // an open Delete dialog) carries over to this one.
  return <SourceFileEditor key={`${slug}/${file}`} slug={slug} file={file} />
}

function SourceFileEditor({ slug, file }: { slug: string; file: string }) {
  const navigate = useNavigate()
  // The model is read before the file, so a save made in between leaves the base
  // older than the text: a save is then refused rather than written over a change
  // this page never showed (EditSourcePage, #1054).
  const loaded = useAsync(async () => {
    const record = await api.getModel(slug)
    return { record, text: await api.getDefinitionFile(slug, { path: file }), version: record.version ?? undefined }
  }, [slug, file])
  const record = loaded.data?.record
  const [text, setText] = useState<string | null>(null)
  const [base, setBase] = useState<string | undefined>()
  /** The model's revision a refused save named as current. */
  const [stale, setStale] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    if (!loaded.data) return
    setText(loaded.data.text)
    setBase(loaded.data.version)
  }, [loaded.data])

  if (file === MAIN_SOURCE) return <Navigate to={sourceFilePath(slug, MAIN_SOURCE)} replace />

  if (!SOURCE_FILE_PATTERN.test(file) || loaded.error) {
    const why = !SOURCE_FILE_PATTERN.test(file) ? `${file} is not a .scad file name.` : loaded.error?.message
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">That file is not here</h1>
        <p className="mt-2 text-[13px] text-muted">{why}</p>
        <Link to={sourceFilePath(slug, MAIN_SOURCE)} className="mt-4 inline-block text-[13px] text-accent underline">
          Open {MAIN_SOURCE}
        </Link>
      </div>
    )
  }

  if (loaded.loading || text === null) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading {file}
      </p>
    )
  }

  const builtin = record?.origin === 'builtin'
  const canEdit = record?.origin === 'mine'
  const dirty = !builtin && loaded.data !== undefined && text !== loaded.data.text

  async function save() {
    try {
      const saved = await api.writeSourceFile(slug, file, text ?? '', base)
      const version = saved.version ?? undefined
      setBase(version)
      setStale(null)
      loaded.setData({ record: loaded.data!.record, text: text ?? '', version }, { supersede: true })
    } catch (caught) {
      // Changed since this file was read: the workbench shows the refusal.
      if (caught instanceof ApiError && caught.status === 409 && caught.problem['current']) {
        setStale(String(caught.problem['current']))
      }
      throw caught
    }
    return sourceFilePath(slug, file)
  }

  async function remove() {
    setDeleteBusy(true)
    setDeleteError(null)
    try {
      await api.deleteSourceFile(slug, file)
      void navigate(sourceFilePath(slug, MAIN_SOURCE), { replace: true })
    } catch (caught) {
      setDeleteError(caught instanceof ApiError ? caught.detail : 'Could not delete the file. Try again.')
      setDeleteBusy(false)
    }
  }

  return (
    <>
      <SourceWorkbench
        breadcrumb={
          <>
            <Link to={modelPath(slug)} className="shrink-0 text-[12px] text-muted hover:text-ink">
              {record?.name ?? slug}
            </Link>
            <span className="text-faint">/</span>
            <h1 className="truncate text-[13px] font-medium">{file}</h1>
          </>
        }
        fields={
          <>
            <SourceFileTabs slug={slug} current={file} canEdit={canEdit}>
              {canEdit && (
                <button
                  type="button"
                  onClick={() => setDeleting(true)}
                  disabled={dirty}
                  title={dirty ? 'Save or discard your edits first.' : undefined}
                  className="rounded-[6px] px-2 py-0.5 text-warn hover:bg-surface-2 disabled:opacity-50"
                >
                  Delete file
                </button>
              )}
            </SourceFileTabs>
            {stale !== null && (
              <div
                data-testid="file-changed-elsewhere"
                role="status"
                className="flex items-center gap-3 border-t border-line bg-accent/8 px-3 py-1.5 text-[12px]"
              >
                <span>
                  This model was changed elsewhere since you opened {file}. Reload it (your edits
                  here are discarded), or keep editing and save over it.
                </span>
                <Button
                  size="sm"
                  onClick={() => {
                    setStale(null)
                    loaded.reload()
                  }}
                >
                  Reload {file}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setBase(stale)
                    setStale(null)
                  }}
                >
                  Keep editing
                </Button>
              </div>
            )}
          </>
        }
        uri={`file:///models/${slug}/${file}`}
        slug={slug}
        source={text}
        onSourceChange={setText}
        saveLabel="Save file"
        canSave={canEdit}
        readOnly={builtin}
        dirty={dirty}
        onSave={save}
      />
      <Dialog
        open={deleting}
        title={`Delete ${file}?`}
        description={`It is removed as one revision, so an older version still has it. If ${MAIN_SOURCE} includes or uses it, the model stops rendering until that line goes.`}
        onClose={() => {
          if (!deleteBusy) setDeleting(false)
        }}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(false)} disabled={deleteBusy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void remove()} disabled={deleteBusy}>
              {deleteBusy && <Spinner />}
              Delete
            </Button>
          </>
        }
      >
        {deleteError && (
          <p role="alert" className="text-[13px] text-warn">
            {deleteError}
          </p>
        )}
      </Dialog>
    </>
  )
}
