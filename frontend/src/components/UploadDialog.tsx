import { useRef, useState, type DragEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { ModelSummary } from '../api/types'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  onClose: () => void
  onUploaded: (model: ModelSummary) => void
}

export function UploadDialog({ open, onClose, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function choose(next: File | undefined) {
    if (!next) return
    if (!next.name.toLowerCase().endsWith('.scad')) {
      setError('That is not a .scad file. ScadBuddy renders OpenSCAD source.')
      setFile(null)
      return
    }
    setError(null)
    setFile(next)
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    choose(event.dataTransfer.files[0])
  }

  async function upload() {
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const model = await api.uploadModel(file)
      onUploaded(model)
      reset()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Upload failed. Try again.')
    } finally {
      setUploading(false)
    }
  }

  function reset() {
    setFile(null)
    setError(null)
    setDragging(false)
    onClose()
  }

  return (
    <Dialog
      open={open}
      title="Add a model"
      description="Drop an OpenSCAD source file. Its customizer parameters are read on upload."
      onClose={reset}
      footer={
        <>
          <Button onClick={reset} disabled={uploading}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void upload()} disabled={!file || uploading}>
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
          {file ? (
            <span className="sb-num text-ink">{file.name}</span>
          ) : (
            'Drag a .scad file here'
          )}
        </p>
        <Button size="sm" onClick={() => inputRef.current?.click()}>
          Choose file
        </Button>
        <input
          ref={inputRef}
          type="file"
          accept=".scad"
          className="sr-only"
          aria-label="OpenSCAD source file"
          onChange={(event) => choose(event.target.files?.[0])}
        />
      </div>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
