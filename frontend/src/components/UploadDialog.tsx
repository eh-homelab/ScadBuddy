import { useEffect, useRef, useState, type DragEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { ModelSummary } from '../api/types'
import {
  classifyFiles,
  droppedFiles,
  folderOf,
  isMarkdown,
  readMetaName,
  readmeProblem,
  thumbnailProblem,
  uploadFilename,
  type ModelFiles,
  type Skipped,
} from '../lib/modelFolder'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  onClose: () => void
  onUploaded: (model: ModelSummary) => void
}

type Extra = 'meta' | 'thumbnail' | 'readme'

export function UploadDialog({ open, onClose, onUploaded }: Props) {
  const [files, setFiles] = useState<ModelFiles | null>(null)
  const [metaName, setMetaName] = useState<string | undefined>(undefined)
  const [ignored, setIgnored] = useState<Skipped[]>([])
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)
  const thumbnailRef = useRef<HTMLInputElement>(null)
  const readmeRef = useRef<HTMLInputElement>(null)

  // Set here rather than as a JSX prop: React's input typings do not know the
  // directory-picker attribute, though every current browser supports it.
  useEffect(() => {
    folderRef.current?.setAttribute('webkitdirectory', '')
  }, [open])

  async function choose(chosen: File[], folder?: string) {
    if (chosen.length === 0) return
    const { files: picked, ignored: unused } = classifyFiles(chosen, folder)
    if (!picked) {
      setError(
        chosen.length === 1
          ? 'That is not a .scad file. ScadBuddy renders OpenSCAD source.'
          : 'There is no .scad file among those. ScadBuddy renders OpenSCAD source.',
      )
      setFiles(null)
      setIgnored([])
      setMetaName(undefined)
      return
    }
    // A folder's own thumbnail.png is held to the same limit as one attached by hand;
    // the rest of the folder still goes up without it.
    const tooLarge = picked.thumbnail ? thumbnailProblem(picked.thumbnail) : null
    // Its README too, against the limit every write path holds a README to.
    const tooLong = picked.readme ? readmeProblem(await picked.readme.text()) : null
    const leftOut = [
      tooLarge && `${tooLarge} ${picked.thumbnail?.name} was left out.`,
      tooLong && `${tooLong} ${picked.readme?.name} was left out.`,
    ].filter(Boolean)
    setError(leftOut.length > 0 ? leftOut.join(' ') : null)
    setFiles({
      ...picked,
      thumbnail: tooLarge ? undefined : picked.thumbnail,
      readme: tooLong ? undefined : picked.readme,
    })
    setIgnored(unused)
    setMetaName(await readMetaName(picked.meta))
  }

  async function attach(kind: 'thumbnail' | 'readme', file: File | undefined) {
    if (!file || !files) return
    const problem =
      kind === 'thumbnail'
        ? thumbnailProblem(file)
        : !isMarkdown(file)
          ? 'The README must be a Markdown (.md) file.'
          : readmeProblem(await file.text())
    if (problem) {
      setError(problem)
      return
    }
    setError(null)
    setFiles({ ...files, [kind]: file })
  }

  function detach(kind: Extra) {
    if (!files) return
    setFiles({ ...files, [kind]: undefined })
    if (kind === 'meta') setMetaName(undefined)
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    void droppedFiles(event.dataTransfer).then(({ files: dropped, folder }) =>
      choose(dropped, folder),
    )
  }

  async function upload() {
    if (!files) return
    setUploading(true)
    setError(null)
    try {
      const model = await api.uploadModel(files.scad, {
        filename: uploadFilename(files, metaName),
        meta: files.meta,
        thumbnail: files.thumbnail,
        readme: files.readme,
      })
      onUploaded(model)
      reset()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Upload failed. Try again.')
    } finally {
      setUploading(false)
    }
  }

  function reset() {
    setFiles(null)
    setMetaName(undefined)
    setIgnored([])
    setError(null)
    setDragging(false)
    onClose()
  }

  return (
    <Dialog
      open={open}
      title="Add a model"
      description="Drop an OpenSCAD source file, or a model folder holding model.scad with its model.json, thumbnail.png and README.md. Its customizer parameters are read on upload."
      onClose={reset}
      footer={
        <>
          <Button onClick={reset} disabled={uploading}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void upload()} disabled={!files || uploading}>
            {uploading && <Spinner />}
            {uploading ? 'Uploading' : 'Add model'}
          </Button>
        </>
      }
    >
      <div
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        data-testid="upload-dropzone"
        className={`flex flex-col items-center justify-center gap-3 rounded-[6px] border border-dashed px-6 py-10 text-center transition-colors ${
          dragging ? 'border-accent bg-accent/8' : 'border-line-strong bg-surface-2'
        }`}
      >
        <p className="text-[13px] text-muted">
          {files ? (
            <span className="sb-num text-ink">
              {files.folder ? `${files.folder}/${files.scad.name}` : files.scad.name}
            </span>
          ) : (
            'Drag a .scad file or a model folder here'
          )}
        </p>
        <div className="flex gap-2">
          <Button size="sm" onClick={() => inputRef.current?.click()}>
            Choose files
          </Button>
          <Button size="sm" onClick={() => folderRef.current?.click()}>
            Choose folder
          </Button>
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".scad,.json,.png,.md"
          className="sr-only"
          aria-label="OpenSCAD source file"
          onChange={(event) => {
            void choose(Array.from(event.target.files ?? []))
            event.target.value = ''
          }}
        />
        <input
          ref={folderRef}
          type="file"
          multiple
          className="sr-only"
          aria-label="Model folder"
          onChange={(event) => {
            const chosen = Array.from(event.target.files ?? [])
            void choose(chosen, folderOf(chosen))
            event.target.value = ''
          }}
        />
      </div>

      {files && (
        <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 text-[13px]">
          <dt className="text-muted">Details</dt>
          <dd className="truncate" data-testid="upload-meta">
            {files.meta ? (
              <>
                <span className="sb-num">{files.meta.name}</span>
                {metaName && <span className="text-muted"> — {metaName}</span>}
              </>
            ) : (
              <span className="text-faint">Named after the file</span>
            )}
          </dd>
          <dd>
            {files.meta && (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Remove model.json"
                onClick={() => detach('meta')}
              >
                Remove
              </Button>
            )}
          </dd>

          <dt className="text-muted">Thumbnail</dt>
          <dd className="truncate" data-testid="upload-thumbnail">
            {files.thumbnail ? (
              <span className="sb-num">{files.thumbnail.name}</span>
            ) : (
              <span className="text-faint">None; the first generated plate stands in</span>
            )}
          </dd>
          <dd>
            {files.thumbnail ? (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Remove thumbnail"
                onClick={() => detach('thumbnail')}
              >
                Remove
              </Button>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => thumbnailRef.current?.click()}>
                Add PNG
              </Button>
            )}
          </dd>

          <dt className="text-muted">README</dt>
          <dd className="truncate" data-testid="upload-readme">
            {files.readme ? (
              <span className="sb-num">{files.readme.name}</span>
            ) : (
              <span className="text-faint">None</span>
            )}
          </dd>
          <dd>
            {files.readme ? (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Remove README"
                onClick={() => detach('readme')}
              >
                Remove
              </Button>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => readmeRef.current?.click()}>
                Add README
              </Button>
            )}
          </dd>
        </dl>
      )}
      <input
        ref={thumbnailRef}
        type="file"
        accept=".png,image/png"
        className="sr-only"
        aria-label="Thumbnail (PNG)"
        onChange={(event) => {
          void attach('thumbnail', event.target.files?.[0])
          event.target.value = ''
        }}
      />
      <input
        ref={readmeRef}
        type="file"
        accept=".md,.markdown,text/markdown"
        className="sr-only"
        aria-label="README (Markdown)"
        onChange={(event) => {
          void attach('readme', event.target.files?.[0])
          event.target.value = ''
        }}
      />

      {ignored.length > 0 && (
        <p className="mt-3 text-[12px] text-muted" data-testid="upload-ignored">
          Not uploaded:{' '}
          {ignored.map(({ name, reason }, index) => (
            <span key={name + index}>
              {index > 0 && ', '}
              <span className="sb-num">{name}</span>
              {reason && <> ({reason})</>}
            </span>
          ))}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
