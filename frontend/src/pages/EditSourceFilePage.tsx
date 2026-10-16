import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router'
import { ApiError, api } from '../api/client'
import { SourceFileTabs } from '../components/SourceFileTabs'
import { SourceWorkbench } from '../components/SourceWorkbench'
import { Hint } from '../components/Hint'
import { PageLoading, PageProblem } from '../components/PageState'
import { Spinner } from '../components/ui/Spinner'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
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
      <PageProblem title="That file is not here" message={why}>
        <Button asChild variant="outline">
          <Link to={sourceFilePath(slug, MAIN_SOURCE)}>Open {MAIN_SOURCE}</Link>
        </Button>
      </PageProblem>
    )
  }

  if (loaded.loading || text === null) {
    return <PageLoading label={`Loading ${file}`} />
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
            <Link to={modelPath(slug)} className="shrink-0 text-sm text-muted-foreground hover:text-foreground">
              {record?.name ?? slug}
            </Link>
            <span className="text-muted-foreground">/</span>
            <h1 className="truncate text-sm font-medium">{file}</h1>
          </>
        }
        fields={
          <>
            <SourceFileTabs slug={slug} current={file} canEdit={canEdit}>
              {canEdit && (
                <Hint content={dirty ? 'Save or discard your edits first.' : undefined} wrap={dirty}>
                  <Button type="button" variant="destructive" size="xs" onClick={() => setDeleting(true)} disabled={dirty}>
                    Delete file
                  </Button>
                </Hint>
              )}
            </SourceFileTabs>
            {stale !== null && (
              <div
                data-testid="file-changed-elsewhere"
                role="status"
                className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-primary/40 bg-primary/10 px-3 py-1.5 text-xs"
              >
                <span>
                  This model was changed elsewhere since you opened {file}. Reload it (your edits
                  here are discarded), or keep editing and save over it.
                </span>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => {
                    setStale(null)
                    loaded.reload()
                  }}
                >
                  Reload {file}
                </Button>
                <Button
                  type="button"
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
      <AlertDialog
        open={deleting}
        onOpenChange={(open) => {
          if (!open && !deleteBusy) setDeleting(false)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Delete ${file}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              {`It is removed as one revision, so an older version still has it. If ${MAIN_SOURCE} includes or uses it, the model stops rendering until that line goes.`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <p role="alert" className="text-sm text-destructive">
              {deleteError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteBusy}>Cancel</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={() => void remove()} disabled={deleteBusy}>
              {deleteBusy && <Spinner />}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
