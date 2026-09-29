import { useEffect, useRef, useState } from 'react'
import { committed, touchAfterRender, waitFor } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { api, ApiError } from '../api/client'
import type {
  Job,
  ModelSummary,
  Output,
  PlateFit,
  PrintRunResult,
  ProjectFile,
  ProjectView,
  SendResult,
} from '../api/types'
import { DownloadBlockedError, downloadBlob, openExternal } from '../lib/embed'
import { fitLabel, fitMessages } from '../lib/plate'
import type { SnapshotOptions } from '../lib/snapshot'
import { useDisplayUnit } from '../lib/units'
import { ColorStrip } from './ColorStrip'
import { ImageDialog } from './ImageDialog'
import { PrintPicker } from './PrintPicker'
import { useProjectList } from '../lib/projects'
import { ProjectPicker } from './ProjectPicker'
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
  /** A high-resolution image of the view to share, from Generate's menu. */
  captureImage: (options: SnapshotOptions) => Promise<Blob | null>
  /** The view's size in CSS pixels. */
  viewSize: () => { width: number; height: number }
  /** The template, so the rendered image can be added to its media. */
  model?: ModelSummary
  onModelChanged?: (model: ModelSummary) => void
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
  captureImage,
  viewSize,
  model,
  onModelChanged,
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
  const [imageOpen, setImageOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /**
   * #317 — the project Generate files the editable 3MF into, shared with the print
   * dialog's picker so both show one choice. Seeded from `last_project_id`; a change on
   * either picker becomes the new `last_project_id`.
   */
  const [projectId, setProjectId] = useState<number | null>(null)
  const [project, setProject] = useState<ProjectView | null>(null)
  /** Fetched once here and shared by both pickers, so the dialog does not list it again. */
  const projects = useProjectList(setProjectId)
  const [filed, setFiled] = useState<{ outputId: string; name: string; file: ProjectFile } | null>(
    null,
  )
  const [fileError, setFileError] = useState<string | null>(null)

  function chooseProject(next: number | null) {
    setProjectId(next)
    // Only a preference: the picker still shows the choice if it is not remembered.
    void api.rememberProject(next).catch(() => undefined)
  }

  /** Best effort: the output is saved whether or not Bambuddy takes the file. */
  async function fileIntoProject(created: Output) {
    if (projectId === null) return
    const name = project?.name ?? `project ${projectId}`
    try {
      const file = await api.fileIntoProject(created.id, projectId)
      setFiled({ outputId: created.id, name, file })
    } catch (cause) {
      setFileError(
        `Saved, but not filed in ${name}: ${cause instanceof ApiError ? cause.detail : 'Bambuddy did not answer.'}`,
      )
    }
  }

  const ready = job?.status === 'done' && !rendering
  const stale = Boolean(output) && output?.id !== undefined && !ready
  const misfit = fit ? fitLabel(fit) : null
  const unit = useDisplayUnit()

  async function generate(): Promise<Output | null> {
    if (!job) return null
    setGenerating(true)
    setError(null)
    setFiled(null)
    setFileError(null)
    try {
      const created = await api.createOutput(slug, job.id)
      const png = await capture()
      if (png) {
        // A missing thumbnail is cosmetic — never fail the generate over it.
        await api.putThumbnail(created.id, png).catch(() => undefined)
      }
      onGenerated(created)
      // After the thumbnail, so the file Bambuddy lists carries the plate image.
      await fileIntoProject(created)
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
      if (kind !== 'send' && live.current.generating) {
        throw new AgentToolError('invalid_args', 'Generate is still filing the project file.')
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
      // Fetched as a blob and saved through lib/embed, so it works inside Bambuddy's
      // sandboxed iframe (a popup that escapes the sandbox, opened before the fetch).
      await downloadBlob(async () => {
        const response = await fetch(api.downloadUrl(output.id))
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return await response.blob()
      }, `${slug}-${output.id}.3mf`)
    } catch (cause) {
      setError(cause instanceof DownloadBlockedError ? cause.message : 'Download failed.')
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
          {!error && output && !stale && filed?.outputId === output.id && (
            <span className="flex min-w-0 items-center gap-1.5 text-[12px]" data-testid="project-filed">
              <span className="truncate text-ok">Saved to {filed.name}</span>
              <button
                type="button"
                onClick={() => openExternal(filed.file.bambuddy_url)}
                className="shrink-0 text-accent underline"
              >
                Open in Bambuddy
              </button>
            </span>
          )}
          {!error && output && !stale && fileError && (
            <span role="alert" className="truncate text-[12px] text-warn">
              {fileError}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* #317 — Generate files the editable 3MF in this project's Bambuddy folder. */}
          <ProjectPicker
            id="customize-project"
            testId="customize-project-select"
            inline
            value={projectId}
            onChange={chooseProject}
            list={projects}
            onProject={setProject}
            disabled={generating}
          />
          <div className="flex">
            <Button
              variant="primary"
              onClick={() => void generate().catch(() => undefined)}
              disabled={!ready || generating}
              data-testid="generate"
              className="rounded-r-none"
            >
              {generating && <Spinner />}
              {generating ? 'Generating' : 'Generate'}
            </Button>
            <GenerateMenu disabled={!ready} onImage={() => setImageOpen(true)} />
          </div>
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
            // #317 — Generate is still filing the project file, which the print reuses.
            disabled={!output || generating}
            data-testid="print"
            title={misfit && fit ? (fitProblems ?? fitMessages(fit, unit)).join('\n') : undefined}
          >
            Print
            {misfit && <span className="text-[12px]">· {misfit}</span>}
          </Button>
        </div>
      </footer>

      <ImageDialog
        open={imageOpen}
        slug={slug}
        captureImage={captureImage}
        viewSize={viewSize}
        model={model}
        onMediaChanged={onModelChanged}
        onClose={() => setImageOpen(false)}
      />

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
        project={{ value: projectId, onChange: chooseProject, list: projects, disabled: generating }}
      />
    </>
  )
}

/** The other things Generate can make from the preview: for now, an image to share. */
function GenerateMenu({ disabled, onImage }: { disabled: boolean; onImage: () => void }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={root} className="relative">
      <Button
        variant="primary"
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
        aria-label="More to generate"
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="generate-menu"
        className="rounded-l-none border-l-accent-ink/25 px-2"
      >
        <svg aria-hidden="true" viewBox="0 0 12 12" className="size-3 fill-current">
          <path d="M2 4.5 6 8.5 10 4.5z" />
        </svg>
      </Button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 bottom-full z-30 mb-1 min-w-48 rounded-[6px] border border-line bg-surface py-1 shadow-xl"
        >
          <button
            type="button"
            role="menuitem"
            data-testid="generate-image"
            className="block w-full px-3 py-1.5 text-left text-[13px] hover:bg-surface-2"
            onClick={() => {
              setOpen(false)
              onImage()
            }}
          >
            Rendered image…
            <span className="block text-[11px] text-faint">A high-resolution PNG of the view</span>
          </button>
        </div>
      )}
    </div>
  )
}
