import {
  useEffect,
  useId,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type Ref,
} from 'react'
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
import type { CameraView } from '../lib/framing'
import type { SnapshotOptions } from '../lib/snapshot'
import { sameJson, type InputsExtra } from '../lib/inputs'
import { saveOutput } from '../lib/saveOutput'
import { traceAction } from '../lib/traceAction'
import { useDisplayUnit } from '../lib/units'
import { ColorStrip } from './ColorStrip'
import { ImageDialog } from './ImageDialog'
import { PrintPicker } from './PrintPicker'
import { useProjectList } from '../lib/projects'
import { ProjectPicker } from './ProjectPicker'
import { SendDialog } from './SendDialog'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'
import { bambuddyLink } from '../lib/bambuddyLinks'

/** What a template UI's `host.openPrint` reaches (spec §4.3). */
export interface ActionBarHandle {
  /** False, and nothing opens, when ``outputId`` is not the output on screen. */
  openPrint(outputId: string): boolean
}

interface Props {
  ref?: Ref<ActionBarHandle>
  slug: string
  job: Job | undefined
  rendering: boolean
  /**
   * #254 — whether `job` is the render of the values on screen. Also gates the
   * Generate button (#754): between a param edit settling and `rendering` flipping
   * true for the new render, `job` and `rendering` still describe the PREVIOUS,
   * already-`done` job — this is the only prop that already knows it is stale.
   */
  upToDate?: boolean
  output: Output | undefined
  /** Captures the preview canvas as the output thumbnail (spec §6). */
  capture: () => Promise<Blob | null>
  /** A high-resolution image of the view to share, from Generate's menu. */
  captureImage: (options: SnapshotOptions) => Promise<Blob | null>
  /** The view's size in CSS pixels. */
  viewSize: () => { width: number; height: number }
  /** #722 — the viewer's camera now, which the image dialog frames a copy of. */
  cameraView?: () => CameraView | null
  /** The template, so the rendered image can be added to its media. */
  model?: ModelSummary
  onModelChanged?: (model: ModelSummary) => void
  /** The UI state recorded with the output (spec 2026-09-27 §4.3). */
  extra: InputsExtra
  /** #81 — whether the model fits the chosen printer, which the Print button warns of. */
  fit: PlateFit | undefined
  /**
   * #289 — every problem the fit check found, each named by its plate when the render
   * has more than one. Without it the tooltip states `fit`'s own, unnamed.
   */
  fitProblems?: string[]
  /** #81 — the model of the printer the print picker has in view. */
  onPrinterModel: (model: string | null) => void
  /** With the UI state (`extra`) the output was saved with. */
  onGenerated: (output: Output, extra: InputsExtra) => void
  onSent: (result: SendResult) => void
  /** A print sliced and queued from the print dialog (spec 2026-09-27). */
  onRan: (result: PrintRunResult) => void
}

export function ActionBar({
  ref,
  slug,
  job,
  rendering,
  upToDate = true,
  output,
  capture,
  captureImage,
  viewSize,
  cameraView,
  model,
  onModelChanged,
  extra,
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
  /** #967 — what the last Generate or download came to, for the polite live region. */
  const [announcement, setAnnouncement] = useState('')
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
  /**
   * #665 — a "Create project" in flight on either picker. Its completion switches the
   * project, so Generate waits for it rather than filing into a project the picker leaves.
   */
  const [pageCreating, setPageCreating] = useState(false)
  const [dialogCreating, setDialogCreating] = useState(false)
  const creatingProject = pageCreating || dialogCreating

  function chooseProject(next: number | null) {
    setProjectId(next)
    // Only a preference: the picker still shows the choice if it is not remembered.
    void api.rememberProject(next).catch(() => undefined)
  }

  /** Best effort: the output is saved whether or not Bambuddy takes the file. */
  /** Files a new output in the remembered project; the file, or null when none was filed. */
  async function fileIntoProject(created: Output): Promise<ProjectFile | null> {
    if (projectId === null) return null
    const name = project?.name ?? `project ${projectId}`
    try {
      const file = await api.fileIntoProject(created.id, projectId)
      setFiled({ outputId: created.id, name, file })
      return file
    } catch (cause) {
      setFileError(
        `Saved, but not filed in ${name}: ${cause instanceof ApiError ? cause.detail : 'Bambuddy did not answer.'}`,
      )
      return null
    }
  }

  useImperativeHandle(
    ref,
    () => ({
      openPrint: (outputId) => {
        if (output?.id !== outputId) return false
        setPrintOpen(true)
        return true
      },
    }),
    [output],
  )

  const ready = job?.status === 'done' && !rendering && upToDate
  const stale = Boolean(output) && output?.id !== undefined && !ready
  const misfit = fit ? fitLabel(fit) : null
  const unit = useDisplayUnit()

  /** The saved output, and the project file Generate filed it as (#931: the agent records both). */
  async function generate(): Promise<{ output: Output; filed: ProjectFile | null; extra: InputsExtra } | null> {
    if (!job) return null
    setGenerating(true)
    setError(null)
    setAnnouncement('')
    setFiled(null)
    setFileError(null)
    try {
      return await traceAction(
        'generate',
        { 'scadbuddy.slug': slug, 'scadbuddy.job_id': job.id },
        async (within, span) => {
          const created = await saveOutput({ slug, job, extra, capture, within })
          span.setAttribute('scadbuddy.output_id', created.id)
          onGenerated(created, extra)
          // After the thumbnail, so the file Bambuddy lists carries the plate image.
          const filed = await within(() => fileIntoProject(created))
          setAnnouncement(
            `Generated ${created.name ?? created.id.slice(0, 8)}. Download 3MF, Send to Bambuddy or Print it.`,
          )
          return { output: created, filed, extra }
        },
      )
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.detail : 'Could not save this output.'
      setError(message)
      setAnnouncement(message)
      throw new AgentToolError('failed', message)
    } finally {
      setGenerating(false)
    }
  }

  const live = useLatest({
    ready,
    generating,
    creatingProject,
    output,
    extra,
    sendOpen,
    printOpen,
  })

  // #254 — Generate, and opening (never confirming) the print and send dialogs.
  useAgentHandlers('actions', {
    generate: async ({ timeout_ms }) => {
      await waitFor(() => (live.current.ready ? true : undefined), {
        timeout: timeout_ms,
        what: 'the preview render to finish',
      })
      if (live.current.generating) throw new AgentToolError('invalid_args', 'Generate is already running.')
      if (live.current.creatingProject) {
        throw new AgentToolError('invalid_args', 'A project is still being created; wait for it first.')
      }
      touchAfterRender(() => document.querySelector('[data-testid="generate"]'))
      const generated = await generate()
      if (!generated) return null
      const { output: created, filed, extra: savedExtra } = generated
      // Shown, or already left behind by a UI-state change made while it saved (#848).
      await committed(
        () => live.current.output?.id === created.id || !sameJson(savedExtra, live.current.extra),
        'the saved output',
      )
      return {
        output: { id: created.id, name: created.name ?? null, slug: created.slug },
        filed: filed && { project_id: filed.project_id, library_file_id: filed.library_file_id, created: filed.created },
      }
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
      if (kind !== 'send' && live.current.creatingProject) {
        throw new AgentToolError('invalid_args', 'A project is still being created.')
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
    if (!output || downloading) return
    setDownloading(true)
    setError(null)
    setAnnouncement('')
    try {
      // Fetched as a blob and saved through lib/embed, so it works inside Bambuddy's
      // sandboxed iframe (a popup that escapes the sandbox, opened before the fetch).
      await downloadBlob(async () => {
        const response = await fetch(api.downloadUrl(output.id))
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return await response.blob()
      }, `${slug}-${output.id}.3mf`)
      setAnnouncement(`Downloaded ${slug}-${output.id}.3mf.`)
    } catch (cause) {
      const message = cause instanceof DownloadBlockedError ? cause.message : 'Download failed.'
      setError(message)
      setAnnouncement(message)
    } finally {
      setDownloading(false)
    }
  }

  return (
    <>
      <footer className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-line bg-surface px-3 py-2">
        {/* #967 — always in the page, so a change to it is announced. */}
        <p className="sr-only" role="status" data-testid="action-status">
          {announcement}
        </p>
        {/*
          Sized by its content (#934): when the status and the buttons do not both fit,
          the buttons wrap to their own row instead of the status running under them,
          and on a bar too narrow for even the status alone its pieces wrap in turn.
        */}
        <div className="flex min-w-0 grow flex-wrap items-center gap-x-3 gap-y-1">
          {job?.colors && job.colors.length > 0 && (
            <>
              <ColorStrip colors={job.colors} />
              <span className="whitespace-nowrap text-[12px] text-muted">
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
                onClick={() => openExternal(bambuddyLink(filed.file.bambuddy_url))}
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
            onCreating={setPageCreating}
          />
          <div className="flex">
            <Button
              variant="primary"
              onClick={() => {
                if (!generating) void generate().catch(() => undefined)
              }}
              // #967 — busy is aria-disabled, not disabled: a disabled button drops the
              // keyboard focus it was pressed with to <body>.
              disabled={!ready || creatingProject}
              aria-disabled={generating || undefined}
              data-testid="generate"
              className="rounded-r-none"
            >
              {generating && <Spinner />}
              {generating ? 'Generating' : 'Generate'}
            </Button>
            <GenerateMenu disabled={!ready} onImage={() => setImageOpen(true)} />
          </div>
          <Button
            onClick={() => void download()}
            disabled={!output}
            aria-disabled={downloading || undefined}
          >
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
            disabled={!output || generating || creatingProject}
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
        cameraView={cameraView}
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
        source={output ? { kind: 'output', output } : undefined}
        onClose={() => setPrintOpen(false)}
        onRan={onRan}
        onPrinterModel={onPrinterModel}
        project={{
          value: projectId,
          onChange: chooseProject,
          list: projects,
          disabled: generating || creatingProject,
          onCreating: setDialogCreating,
        }}
      />
    </>
  )
}

/**
 * The other things Generate can make from the preview: for now, an image to share.
 *
 * #968 — the WAI-ARIA menu button pattern. Opening it (click, Enter, Space or the
 * arrows) puts focus on an item; the arrows, Home and End move between items, which are
 * out of the Tab order; Escape closes it back to the button, and Tab out closes it.
 */
function GenerateMenu({ disabled, onImage }: { disabled: boolean; onImage: () => void }) {
  // Which item takes focus as it opens: the first, or the last for ArrowUp.
  const [open, setOpen] = useState<'first' | 'last' | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const menuId = useId()

  const items = () => Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
  const close = (refocus: boolean) => {
    setOpen(null)
    // The shared Button takes no ref, so the trigger is found in the root.
    if (refocus) root.current?.querySelector<HTMLButtonElement>('[aria-haspopup="menu"]')?.focus()
  }

  useEffect(() => {
    if (!open) return
    const all = items()
    const first = open === 'last' ? all.at(-1) : all[0]
    first?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(null)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      // Back to the button only from inside: an Escape elsewhere just closes it.
      close(!!root.current?.contains(document.activeElement))
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  function onTriggerKey(event: ReactKeyboardEvent) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    setOpen(event.key === 'ArrowUp' ? 'last' : 'first')
  }

  function onMenuKey(event: ReactKeyboardEvent) {
    const all = items()
    const at = all.indexOf(document.activeElement as HTMLElement)
    const next =
      event.key === 'ArrowDown'
        ? all[(at + 1) % all.length]
        : event.key === 'ArrowUp'
          ? all[(at - 1 + all.length) % all.length]
          : event.key === 'Home'
            ? all[0]
            : event.key === 'End'
              ? all.at(-1)
              : undefined
    if (event.key === 'Tab') {
      // Focus goes on to wherever Tab takes it; the menu does not stay open behind.
      setOpen(null)
      return
    }
    if (!next) return
    event.preventDefault()
    next.focus()
  }

  return (
    <div ref={root} className="relative">
      <Button
        variant="primary"
        onClick={() => setOpen((value) => (value ? null : 'first'))}
        onKeyDown={onTriggerKey}
        disabled={disabled}
        aria-label="More to generate"
        aria-haspopup="menu"
        aria-expanded={open !== null}
        aria-controls={open ? menuId : undefined}
        data-testid="generate-menu"
        className="rounded-l-none border-l-accent-ink/25 px-2"
      >
        <svg aria-hidden="true" viewBox="0 0 12 12" className="size-3 fill-current">
          <path d="M2 4.5 6 8.5 10 4.5z" />
        </svg>
      </Button>
      {open && (
        <div
          ref={menu}
          id={menuId}
          role="menu"
          aria-label="More to generate"
          onKeyDown={onMenuKey}
          className="absolute right-0 bottom-full z-30 mb-1 min-w-48 rounded-[6px] border border-line bg-surface py-1 shadow-xl"
        >
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            data-testid="generate-image"
            className="block w-full px-3 py-1.5 text-left text-[13px] outline-none hover:bg-surface-2 focus-visible:bg-surface-2"
            onClick={() => {
              // Back to the button first, so the dialog returns focus there when it closes.
              close(true)
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
