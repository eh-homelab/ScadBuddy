import { useState } from 'react'
import { entityApi } from '../../entities/api'
import { Input } from '@/components/ui/input'

/**
 * #8062 — a photo or a file for a task's answer. It uploads to the task's entity
 * (`task:<id>`, R4) and the answer is the attachment's id. A phone offers its camera for
 * a photo (`accept` of images).
 */
export function UploadField({
  id,
  taskId,
  accept,
  capture,
  common,
  onChange,
}: {
  id?: string
  taskId: string
  accept?: string
  capture?: boolean
  common?: Record<string, unknown>
  onChange: (attachmentId: string) => void
}) {
  const [state, setState] = useState<{ busy: boolean; name?: string; error?: string }>({ busy: false })

  async function pick(file: File | undefined) {
    if (!file) return
    setState({ busy: true, name: file.name })
    onChange('')
    try {
      const attachment = await entityApi.addAttachment(`task:${taskId}`, file, { role: 'answer' })
      onChange(attachment.id)
      setState({ busy: false, name: attachment.filename || file.name })
    } catch (caught) {
      setState({ busy: false, error: caught instanceof Error ? caught.message : String(caught) })
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Input
        {...common}
        id={id}
        type="file"
        accept={accept}
        capture={capture ? 'environment' : undefined}
        className="h-11 py-2 text-base"
        disabled={state.busy}
        onChange={(event) => void pick(event.target.files?.[0])}
      />
      {state.busy && (
        <p role="status" className="text-[12px] text-muted-foreground">
          Uploading {state.name}…
        </p>
      )}
      {!state.busy && state.name && !state.error && (
        <p role="status" className="text-[12px] break-all text-muted-foreground">
          Uploaded {state.name}
        </p>
      )}
      {state.error && (
        <p role="alert" className="text-[12px] break-words text-destructive">
          {state.error}
        </p>
      )}
    </div>
  )
}
