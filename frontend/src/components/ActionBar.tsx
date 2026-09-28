import { useState } from 'react'
import { committed, touchAfterRender, waitFor } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { api, ApiError } from '../api/client'
import type { Job, Output, PlateFit, PrintRunResult, SendResult } from '../api/types'
import { triggerDownload } from '../lib/embed'
import { fitLabel, fitMessages } from '../lib/plate'
import { useDisplayUnit } from '../lib/units'
import { ColorStrip } from './ColorStrip'
import { PrintPicker } from './PrintPicker'
import { SendDialog } from './SendDialog'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  job: Job | undefined
  rendering: boolean
  /**
   * #254 — whether `job` is the render of the values on screen. Only an agent's
   * `generate` reads it: a person cannot press Generate in the frame where it is not.
   */
  upToDate?: boolean
  output: Output | undefined
  /** Captures the preview canvas as the output thumbnail (spec §6). */
  capture: () => Promise<Blob | null>
  /** #81 — whether the model fits the chosen printer, which the Print button warns of. */
  fit: PlateFit | undefined
  /**
   * #289 — every problem the fit check found, each named by its plate when the render
   * has more than one. Without it the tooltip states `fit`'s own, unnamed.
   */
  fitProblems?: string[]
  /** #81 — the model of the printer the print picker has in view. */
  onPrinterModel: (model: string | null) => void
  onGenerated: (output: Output) => void
  onSent: (result: SendResult) => void
  /** A print sliced and queued from the print dialog (spec 2026-09-27). */
  onRan: (result: PrintRunResult) => void
}

export function ActionBar({
  slug,
  job,
  rendering,
  upToDate = true,
  output,
  capture,
  fit,
  fitProblems,
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
  const misfit = fit ? fitLabel(fit) : null
  const unit = useDisplayUnit()

  async function generate(): Promise<Output | null> {
    if (!job) return null
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
      return created
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.detail : 'Could not save this output.'
      setError(message)
      throw new AgentToolError('failed', message)
    } finally {
      setGenerating(false)
    }
  }

  const live = useLatest({ ready: ready && upToDate, generating, output, sendOpen, printOpen })

  // #254 — Generate, and opening (never confirming) the print and send dialogs.
  useAgentHandlers('actions', {
    generate: async ({ timeout_ms }) => {
      await waitFor(() => (live.current.ready ? true : undefined), {
        timeout: timeout_ms,
        what: 'the preview render to finish',
      })
      if (live.current.generating) throw new AgentToolError('invalid_args', 'Generate is already running.')
      touchAfterRender(() => document.querySelector('[data-testid="generate"]'))
      const created = await generate()
      if (!created) return null
      await committed(() => live.current.output?.id === created.id, 'the saved output')
      return { output: { id: created.id, name: created.name ?? null } }
    },
    open_print_dialog: async ({ kind }) => {
      if (!live.current.output) {
        throw new AgentToolError(
          'invalid_args',
          'There is no generated output for these values yet; call generate first.',
        )
      }
      if (kind === 'send') setSendOpen(true)
      else setPrintOpen(true)
      await committed(() => (kind === 'send' ? live.current.sendOpen : live.current.printOpen), 'the dialog to open')
      touchAfterRender(() => document.querySelector('[role="dialog"]'))
      return {
        opened: kind === 'send' ? 'Send to Bambuddy' : 'Print',
        note: 'The dialog is open for the user to review. Only the user can confirm it.',
      }
    },
  })

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
            onClick={() => void generate().catch(() => undefined)}
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
            variant={misfit ? 'danger' : 'default'}
            onClick={() => setPrintOpen(true)}
            disabled={!output}
            data-testid="print"
            title={misfit && fit ? (fitProblems ?? fitMessages(fit, unit)).join('\n') : undefined}
          >
            Print
            {misfit && <span className="text-[12px]">· {misfit}</span>}
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
