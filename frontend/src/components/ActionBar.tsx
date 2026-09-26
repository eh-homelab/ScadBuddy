import { useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Job, Output, Plate, PrintRunResult, SendResult } from '../api/types'
import { triggerDownload } from '../lib/embed'
import { describeOvershoot, type Overshoot } from '../lib/plate'
import { ColorStrip } from './ColorStrip'
import { PrintPicker } from './PrintPicker'
import { SendDialog } from './SendDialog'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  job: Job | undefined
  rendering: boolean
  output: Output | undefined
  /** Captures the preview canvas as the output thumbnail (spec §6). */
  capture: () => Promise<Blob | null>
  /** #81 — the axes the model overflows the plate on, which the Print button warns of. */
  overshoot: Overshoot[]
  plate: Plate | undefined
  /** #81 — the model of the printer the print picker has in view. */
  onPrinterModel: (model: string | null) => void
  onGenerated: (output: Output) => void
  onSent: (result: SendResult) => void
  /** #86 — a pipeline run started from the print picker. */
  onRan: (result: PrintRunResult) => void
}

export function ActionBar({
  slug,
  job,
  rendering,
  output,
  capture,
  overshoot,
  plate,
  onPrinterModel,
  onGenerated,
  onSent,
  onRan,
}: Props) {
  const [generating, setGenerating] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [sendOpen, setSendOpen] = useState(false)
  const [printOpen, setPrintOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const ready = job?.status === 'done' && !rendering
  const stale = Boolean(output) && output?.id !== undefined && !ready

  async function generate() {
    if (!job) return
    setGenerating(true)
    setError(null)
    try {
      const created = await api.createOutput(slug, job.id)
      const png = await capture()
      if (png) {
        // A missing thumbnail is cosmetic — never fail the generate over it.
        await api.putThumbnail(created.id, png).catch(() => undefined)
      }
      onGenerated(created)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save this output.')
    } finally {
      setGenerating(false)
    }
  }

  async function download() {
    if (!output) return
    setDownloading(true)
    setError(null)
    try {
      // Fetched as a blob so the download works from inside Bambuddy's sandboxed iframe.
      const response = await fetch(api.downloadUrl(output.id))
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      triggerDownload(url, `${slug}-${output.id}.3mf`)
      setTimeout(() => URL.revokeObjectURL(url), 30_000)
    } catch {
      setError('Download failed.')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <>
      <footer className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-line bg-surface px-3 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          {job?.colors && job.colors.length > 0 && (
            <>
              <ColorStrip colors={job.colors} />
              <span className="text-[12px] text-muted">
                {job.colors.length === 1 ? '1 colour' : `${job.colors.length} colours`}
              </span>
            </>
          )}
          {error && (
            <span role="alert" className="truncate text-[12px] text-warn">
              {error}
            </span>
          )}
          {!error && output && !stale && (
            <span className="truncate text-[12px] text-ok">
              Saved {output.name ?? output.id.slice(0, 8)}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            onClick={() => void generate()}
            disabled={!ready || generating}
            data-testid="generate"
          >
            {generating && <Spinner />}
            {generating ? 'Generating' : 'Generate'}
          </Button>
          <Button onClick={() => void download()} disabled={!output || downloading}>
            {downloading && <Spinner />}
            Download 3MF
          </Button>
          <Button onClick={() => setSendOpen(true)} disabled={!output}>
            Send to Bambuddy
          </Button>
          <Button
            variant={overshoot.length > 0 ? 'danger' : 'default'}
            onClick={() => setPrintOpen(true)}
            disabled={!output}
            data-testid="print"
            title={
              plate && overshoot.length > 0
                ? overshoot.map((over) => describeOvershoot(over, plate)).join('\n')
                : undefined
            }
          >
            Print
            {overshoot.length > 0 && (
              <span className="text-[12px]">
                · Too big on {overshoot.map((over) => over.axis).join(', ')}
              </span>
            )}
          </Button>
        </div>
      </footer>

      <SendDialog
        open={sendOpen}
        output={output}
        onClose={() => setSendOpen(false)}
        onSent={onSent}
      />

      <PrintPicker
        open={printOpen}
        slug={slug}
        output={output}
        onClose={() => setPrintOpen(false)}
        onRan={onRan}
        onPrinterModel={onPrinterModel}
      />
    </>
  )
}
