import { useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from 'react'
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
import { ExtraOutputsError, saveOutput, saveRemaining } from '../lib/saveOutput'
import { traceAction } from '../lib/traceAction'
import { PHONE_QUERY, useMediaQuery } from '../lib/useMediaQuery'
import { useDisplayUnit } from '../lib/units'
import { ColorStrip } from './ColorStrip'
import { AddToOrderDialog } from './orders/AddToOrderDialog'
import { ColoursDialog } from './ColoursDialog'
import { ImageDialog } from './ImageDialog'
import { PrintPicker } from './PrintPicker'
import { breadcrumbs, useProjectList } from '../lib/projects'
import { useSubscription } from '../lib/realtime'
import { useAsync } from '../lib/useAsync'
import { ProjectPicker } from './ProjectPicker'
import { SendDialog } from './SendDialog'
import { Hint } from './Hint'
import { Spinner } from './ui/spinner'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ChevronDownIcon, ChevronUpIcon } from 'lucide-react'
import { bambuddyLink } from '../lib/bambuddyLinks'

/** #1289 — the most colours the breakdown draws (backend `MAX_BREAKDOWN_COLOURS`). */
const MAX_BREAKDOWN_COLOURS = 16

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
  /** #2444 — why there is nothing to generate: a value on the form to fix. Said on Generate. */
  blockedReason?: string
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
  /**
   * #1660 — the preset in use, or none. The project Generate files into is remembered
   * per model and preset, and the picker moves to it when either changes.
   */
  preset?: { id: string; name: string } | null
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
  blockedReason,
  output,
  capture,
  captureImage,
  viewSize,
  cameraView,
  model,
  preset = null,
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
  const [coloursOpen, setColoursOpen] = useState(false)
  /** #4249 — "Add to order…": the saved output of the values on screen becomes a line item. */
  const [orderOpen, setOrderOpen] = useState(false)
  /** #1741 — at a phone's width the project, Download, Send and Print wait behind More. */
  const phone = useMediaQuery(PHONE_QUERY)
  const [moreOpen, setMoreOpen] = useState(false)
  const dialogOpen = sendOpen || printOpen || imageOpen || coloursOpen || orderOpen
  const [error, setError] = useState<string | null>(null)
  /** A job whose first output was saved but not the rest: Generate saves only those. */
  const [unfinished, setUnfinished] = useState<ExtraOutputsError | null>(null)
  /** Stops a render Generate is waiting for when the page leaves this model. */
  const generation = useRef<AbortController | null>(null)
  useEffect(() => () => generation.current?.abort(), [slug])
  /** #967 — what the last Generate or download came to, for the polite live region. */
  const [announcement, setAnnouncement] = useState('')
  /**
   * #317 — the project Generate files the editable 3MF into, shared with the print
   * dialog's picker so both show one choice. It opens on the project remembered for this
   * model (#1660), never on another model's. A change on either picker applies to this
   * page only: the default project (`last_project_id`) changes only in Settings (#3188).
   */
  const [projectId, setProjectId] = useState<number | null>(null)
  const [project, setProject] = useState<ProjectView | null>(null)
  /**
   * #3651 — a library folder of no project chosen instead of a project: Generate files
   * the 3MF there. Not remembered per model, as a project is: a folder holds no prints.
   */
  const [folderId, setFolderId] = useState<number | null>(null)
  /** Fetched once here and shared by both pickers, so the dialog does not list it again. */
  const projects = useProjectList()
  const listed = projects.choices?.projects
  const paths = useMemo(() => breadcrumbs(listed ?? []), [listed])
  const projectPath = (id: number) => paths.get(id) ?? `project ${id}`
  const projectName = (id: number) => listed?.find((entry) => entry.id === id)?.name ?? `project ${id}`
  /** One no longer in Bambuddy's list is nothing remembered. */
  const stillListed = (id: number | null) =>
    id !== null && (!listed || listed.some((entry) => entry.id === id)) ? id : null
  // Tagged with the model they were read for: a write that lands after the page moved
  // to another model (a filing's PUT) must not pass for the new model's (#2142 review).
  const modelProjects = useAsync(async () => ({ slug, ...(await api.getModelProjects(slug)) }), [slug])
  /** This model's remembered projects, once read; never another model's. */
  const loadedProjects = modelProjects.data?.slug === slug ? modelProjects.data : undefined
  // Another tab (or the agent) filing this model, or a Forget in Settings, changes what
  // is remembered: read it again, or this tab confirms against a stale project.
  useSubscription('settings', (signal, confirmation) => {
    if (confirmation) {
      // Only if something passed since the first read (#1039).
      void modelProjects.staleFor('settings', confirmation).then((stale) => {
        if (stale) modelProjects.refresh()
      })
      return
    }
    const section = signal === 'resync' ? null : signal.data.section
    if (signal === 'resync' || section === 'model_project' || section === 'remembered') modelProjects.refresh()
  })
  const rememberedFor = (presetId: string | null) =>
    stillListed(
      loadedProjects?.projects?.find((entry) => (entry.preset_id ?? null) === presetId)?.project_id ?? null,
    )
  /**
   * #1660 — the project this model was last filed into with the preset in use, which
   * Generate asks before filing elsewhere. Only that pair's: a preset never filed yet was
   * not "last filed" anywhere.
   */
  const remembered = rememberedFor(preset?.id ?? null)
  /**
   * Where the picker opens: the pair's project, else the model's own with no preset (the
   * project its content is in, #3651), else the project Settings names as the default.
   */
  const settingsDefault = stillListed(projects.choices?.last_project_id ?? null)
  const opensOn = remembered ?? (preset ? rememberedFor(null) : null) ?? settingsDefault
  /** What the model is called in the confirmation: the preset in use, else the model. */
  const subject = preset?.name ?? model?.name ?? slug
  // #1660 — opening a model or loading a preset moves the picker to its project, or with
  // none remembered to the Settings default, else no project: `last_project_id` is another
  // model's (the 2026-10-11 QA report filed an apple into a keychain project). Once per
  // change, so a pick made since stays. After the list, which names the default.
  const preselectKeyFor = (presetId: string | null, target: number | null) =>
    `${slug}\n${presetId ?? ''}\n${target ?? ''}`
  const preselectKey =
    loadedProjects && !projects.loading ? preselectKeyFor(preset?.id ?? null, opensOn) : null
  const [preselected, setPreselected] = useState<string | null>(null)
  if (preselectKey !== null && preselectKey !== preselected) {
    setPreselected(preselectKey)
    setProjectId(opensOn)
    setFolderId(null)
  }
  const latestPresetId = useLatest(preset?.id ?? null)
  const latestSlug = useLatest(slug)
  /** #1660 — Generate about to file somewhere other than the remembered project, asking. */
  const [confirming, setConfirming] = useState<{ remembered: number; chosen: number } | null>(null)
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
    setFolderId(null)
  }

  /** #3651 — a folder in place of a project; `null` is a project chosen instead. */
  function chooseFolder(next: number | null) {
    setFolderId(next)
    if (next !== null) setProjectId(null)
  }
  const folderChoice = { value: folderId, onChange: chooseFolder }

  /** #1660 — the remembered project, when Generate into `target` would file elsewhere. */
  function conflict(target: number | null): { remembered: number; chosen: number } | null {
    return remembered !== null && target !== null && target !== remembered ? { remembered, chosen: target } : null
  }

  /**
   * Files a new output in `target`; the file, or null when none was filed. Best effort:
   * the output is saved whether or not Bambuddy takes the file. A filed output's project
   * becomes the one remembered for this model and preset (#1660).
   */
  async function fileIntoProject(
    created: Output,
    target: number | null,
    folder: number | null,
  ): Promise<ProjectFile | null> {
    if (target === null) return folder === null ? null : fileIntoFolder(created, folder)
    const name = target === projectId && project ? project.name : projectName(target)
    try {
      const file = await api.fileIntoProject(created.id, target)
      setFiled({ outputId: created.id, name, file })
      const presetId = preset?.id ?? null
      void api
        .putModelProject(slug, { preset_id: presetId, preset_name: preset?.name ?? null, project_id: target })
        .then((next) => {
          // The page has moved to another model since: this is not its projects.
          if (latestSlug.current !== slug) return
          // What this page just filed is not a fresh preselect: a pick made on the
          // unlocked picker since Generate ended stays where it was put.
          if (latestPresetId.current === presetId) setPreselected(preselectKeyFor(presetId, target))
          modelProjects.setData({ slug, ...next }, { supersede: true })
        })
        .catch(() => undefined)
      return file
    } catch (cause) {
      setFileError(
        `Saved, but not filed in ${name}: ${cause instanceof ApiError ? cause.detail : 'Bambuddy did not answer.'}`,
      )
      return null
    }
  }

  /** #3651 — the same into a library folder of no project; nothing is remembered for it. */
  async function fileIntoFolder(created: Output, folder: number): Promise<ProjectFile | null> {
    const name = projects.choices?.folders?.find((entry) => entry.id === folder)?.path ?? `folder ${folder}`
    try {
      const file = await api.fileIntoFolder(created.id, folder)
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

  /**
   * The saved output, and the project file Generate filed it as (#931: the agent records
   * both). `target` is the project it files into: the picker's, unless a confirmation
   * (#1660) chose another.
   */
  async function generate(
    target: number | null = projectId,
    // #3651 — the folder it files into when `target` is no project.
    folder: number | null = folderId,
  ): Promise<{ output: Output; filed: ProjectFile | null; extra: InputsExtra } | null> {
    if (!job) return null
    const controller = new AbortController()
    generation.current?.abort()
    generation.current = controller
    setGenerating(true)
    setError(null)
    // Matched on the job Generate was asked to save, not a re-render it made for it.
    const resume = unfinished?.requested.id === job.id ? unfinished : null
    setUnfinished(null)
    setAnnouncement('')
    setFiled(null)
    setFileError(null)
    try {
      return await traceAction(
        'generate',
        { 'scadbuddy.slug': slug, 'scadbuddy.job_id': job.id },
        async (within, span) => {
          let created: Output
          if (resume) {
            await saveRemaining(resume)
            created = resume.saved
          } else {
            // The first output shows as soon as it is saved, before a pipeline job's others.
            created = await saveOutput({
              slug,
              job,
              extra,
              capture,
              onSaved: (saved) => onGenerated(saved, extra),
              signal: controller.signal,
              within,
            })
          }
          span.setAttribute('scadbuddy.output_id', created.id)
          // After the thumbnail, so the file Bambuddy lists carries the plate image.
          const filed = await within(() => fileIntoProject(created, target, folder))
          setAnnouncement(
            `Generated ${created.name ?? created.id.slice(0, 8)}. Download 3MF, Send to Bambuddy or Print it.`,
          )
          return { output: created, filed, extra }
        },
      )
    } catch (cause) {
      if (controller.signal.aborted) return null // the page has moved on
      if (cause instanceof ExtraOutputsError) setUnfinished(cause)
      const message =
        cause instanceof ExtraOutputsError
          ? cause.message
          : cause instanceof ApiError
            ? cause.detail
            : 'Could not save this output.'
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
    projectId,
    folderId,
    conflict,
    subject,
    projectPath,
    listed,
  })

  // #254 — Generate, and opening (never confirming) the print and send dialogs.
  useAgentHandlers('actions', {
    generate: async ({ timeout_ms, project_id }) => {
      await waitFor(() => (live.current.ready ? true : undefined), {
        timeout: timeout_ms,
        what: 'the preview render to finish',
      })
      if (live.current.generating) throw new AgentToolError('invalid_args', 'Generate is already running.')
      if (live.current.creatingProject) {
        throw new AgentToolError('invalid_args', 'A project is still being created; wait for it first.')
      }
      // #1660 — the same check the button asks: filing somewhere other than the project
      // this model and preset were last filed in needs the project named.
      const target = project_id === undefined ? live.current.projectId : project_id
      const clash = project_id === undefined ? live.current.conflict(target) : null
      if (clash) {
        const { subject: named, projectPath: path } = live.current
        throw new AgentToolError(
          'invalid_args',
          `${named} was last filed in project ${clash.remembered} (${path(clash.remembered)}), but the Project ` +
            `picker has project ${clash.chosen} (${path(clash.chosen)}). Nothing was saved. Call generate again ` +
            `with project_id ${clash.chosen} to file it in ${path(clash.chosen)}, or ${clash.remembered} to file ` +
            `it in ${path(clash.remembered)}.`,
        )
      }
      // An id Bambuddy does not list would be saved and then fail to file: refuse it first.
      if (project_id !== undefined && project_id !== null && !live.current.listed?.some((p) => p.id === project_id)) {
        throw new AgentToolError(
          'invalid_args',
          live.current.listed
            ? `Bambuddy has no project ${project_id}. Nothing was saved. Call generate with a project_id from ` +
                'list_print_projects, or null for no project.'
            : `The project list has not been read, so project ${project_id} cannot be checked. Nothing was saved.`,
        )
      }
      if (project_id !== undefined) chooseProject(project_id)
      touchAfterRender(() => document.querySelector('[data-testid="generate"]'))
      // A project the agent named (or its "no project") is no folder.
      const generated = await generate(target, project_id === undefined ? live.current.folderId : null)
      if (!generated) return null
      const { output: created, filed, extra: savedExtra } = generated
      // Shown, or already left behind by a UI-state change made while it saved (#848).
      await committed(
        () => live.current.output?.id === created.id || !sameJson(savedExtra, live.current.extra),
        'the saved output',
      )
      const result = {
        output: { id: created.id, name: created.name ?? null, slug: created.slug },
        filed: filed && { project_id: filed.project_id, library_file_id: filed.library_file_id, created: filed.created },
      }
      if (live.current.output?.id === created.id) return result
      // Saved, but the page has moved on: open_print_dialog would refuse it (#1471).
      return {
        ...result,
        superseded: true,
        note:
          'The template UI state changed while this output saved, so it is not the one on screen. ' +
          'Generate again once the inputs are settled before opening the print or send dialog.',
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
      touchAfterRender(() => document.querySelector('[data-modal]'))
      return {
        opened: kind === 'send' ? 'Send to Bambuddy' : 'Print',
        note: 'The dialog is open for the user to review. Only the user can confirm it.',
      }
    },
  })

  // More stays open under one of its dialogs, which takes its own Escape and its own
  // presses: what closes More is Escape, or a press outside it, with no dialog up.
  const keepMore = (event: Event) => {
    if (dialogOpen) event.preventDefault()
  }

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

  // #317 — Generate files the editable 3MF in this project's Bambuddy folder.
  const projectPicker = (
    <ProjectPicker
      id="customize-project"
      testId="customize-project-select"
      inline
      value={projectId}
      onChange={chooseProject}
      folder={folderChoice}
      list={projects}
      onProject={setProject}
      // #666 — locked while Generate files, so the project on screen is where it went.
      disabled={generating || confirming !== null}
      onCreating={setPageCreating}
    />
  )
  const generateButtons = (
    <div className="flex items-center">
      <Button
        type="button"
        onClick={() => {
          if (generating) return
          // #1660 — filing somewhere other than the remembered project asks first.
          const clash = conflict(projectId)
          if (clash) setConfirming(clash)
          else void generate().catch(() => undefined)
        }}
        // #967 — busy is aria-disabled, not disabled: a disabled button drops the
        // keyboard focus it was pressed with to <body>.
        disabled={!ready || creatingProject}
        aria-disabled={generating || undefined}
        data-testid="generate"
        aria-describedby={blockedReason ? `${slug}-generate-blocked` : undefined}
        className="h-9 rounded-r-none px-3"
      >
        {generating && <Spinner />}
        {generating ? 'Generating' : 'Generate'}
      </Button>
      <GenerateMenu
        disabled={!ready}
        onImage={() => setImageOpen(true)}
        // #1289 — a breakdown says something only with two colours or more, and the
        // backend draws at most 16. The backend counts the colours the GLB's parts
        // carry, which can exceed the job's extruder colours, so a job offered here
        // can still get a 422 past the cap; the dialog shows that refusal.
        onColours={
          job?.colors && job.colors.length > 1 && job.colors.length <= MAX_BREAKDOWN_COLOURS
            ? () => setColoursOpen(true)
            : undefined
        }
        onOrder={output ? () => setOrderOpen(true) : undefined}
      />
      {blockedReason && (
        <span id={`${slug}-generate-blocked`} data-testid="generate-blocked" className="ml-2 text-xs text-destructive">
          {blockedReason}
        </span>
      )}
    </div>
  )
  const downloadButton = (
    <Button
      type="button"
      variant="outline"
      className="h-9"
      onClick={() => void download()}
      disabled={!output}
      aria-disabled={downloading || undefined}
    >
      {downloading && <Spinner />}
      Download 3MF
    </Button>
  )
  const sendButton = (
    <Button type="button" variant="outline" className="h-9" onClick={() => setSendOpen(true)} disabled={!output}>
      Send to Bambuddy
    </Button>
  )
  // #317 — Generate is still filing the project file, which the print reuses.
  const printDisabled = !output || generating || creatingProject
  const printButton = (
    <Hint content={misfit && fit ? (fitProblems ?? fitMessages(fit, unit)).join('\n') : undefined} wrap={printDisabled}>
      <Button
        type="button"
        variant={misfit ? 'destructive' : 'outline'}
        className="h-9"
        onClick={() => setPrintOpen(true)}
        disabled={printDisabled}
        data-testid="print"
      >
        Print
        {misfit && <span className="text-xs">· {misfit}</span>}
      </Button>
    </Hint>
  )

  return (
    <>
      <footer className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t bg-background px-3 py-2">
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
              {/* #1741 — the swatches alone on a phone, so the bar stays one row. */}
              <span className={phone ? 'sr-only' : 'whitespace-nowrap text-xs text-muted-foreground'}>
                {job.colors.length === 1 ? '1 colour' : `${job.colors.length} colours`}
              </span>
            </>
          )}
          {error && (
            <span role="alert" className="truncate text-xs text-destructive">
              {error}
            </span>
          )}
          {!error && output && !stale && (
            <span className="truncate text-xs text-ok">
              Saved {output.name ?? output.id.slice(0, 8)}
            </span>
          )}
          {!error && output && !stale && filed?.outputId === output.id && (
            <span className="flex min-w-0 items-center gap-1.5 text-xs" data-testid="project-filed">
              <span className="truncate text-ok">Saved to {filed.name}</span>
              {/* #4507 — a native folder's file has no Bambuddy page. */}
              {filed.file.bambuddy_url && (
                <Button
                  type="button"
                  variant="link"
                  size="xs"
                  onClick={() => openExternal(bambuddyLink(filed.file.bambuddy_url as string))}
                  className="h-auto shrink-0 px-0"
                >
                  Open in Bambuddy
                </Button>
              )}
            </span>
          )}
          {!error && output && !stale && fileError && (
            <span role="alert" className="truncate text-xs text-destructive">
              {fileError}
            </span>
          )}
        </div>

        {phone ? (
          // #1741 — one row on a phone: Generate, and the rest behind More.
          <div className="flex shrink-0 items-center gap-2">
            {generateButtons}
            <Popover open={moreOpen} onOpenChange={setMoreOpen}>
              <PopoverTrigger asChild>
                <Button
                  type="button"
                  // Print's "does not fit" warning, while Print is out of sight.
                  variant={misfit ? 'destructive' : 'outline'}
                  className="h-9"
                  data-testid="more-actions"
                >
                  More
                  {moreOpen ? <ChevronDownIcon /> : <ChevronUpIcon />}
                </Button>
              </PopoverTrigger>
              <PopoverContent
                side="top"
                align="end"
                aria-label="More actions"
                data-testid="more-actions-panel"
                collisionPadding={12}
                onEscapeKeyDown={keepMore}
                onInteractOutside={keepMore}
                className="max-h-[60vh] w-[calc(100vw-1.5rem)] gap-3 overflow-y-auto"
              >
                {projectPicker}
                <div className="flex flex-wrap items-center gap-2">
                  {downloadButton}
                  {sendButton}
                  {printButton}
                </div>
              </PopoverContent>
            </Popover>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {projectPicker}
            {generateButtons}
            {downloadButton}
            {sendButton}
            {printButton}
          </div>
        )}
      </footer>

      <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>File in another project?</AlertDialogTitle>
            {confirming && (
              <AlertDialogDescription data-testid="generate-project-confirm">
                {subject} was last filed in <strong>{projectPath(confirming.remembered)}</strong>. File this in{' '}
                <strong>{projectPath(confirming.chosen)}</strong>?
              </AlertDialogDescription>
            )}
          </AlertDialogHeader>
          {confirming && (
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setConfirming(null)
                  chooseProject(confirming.remembered)
                  void generate(confirming.remembered).catch(() => undefined)
                }}
                data-testid="generate-use-remembered"
              >
                Use {projectName(confirming.remembered)}
              </Button>
              <Button
                type="button"
                onClick={() => {
                  setConfirming(null)
                  void generate(confirming.chosen).catch(() => undefined)
                }}
                data-testid="generate-file-chosen"
              >
                File in {projectName(confirming.chosen)}
              </Button>
            </AlertDialogFooter>
          )}
        </AlertDialogContent>
      </AlertDialog>

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

      <ColoursDialog
        open={coloursOpen}
        jobId={job?.id}
        colors={job?.colors ?? []}
        onClose={() => setColoursOpen(false)}
      />

      {output && <AddToOrderDialog open={orderOpen} outputId={output.id} outputName={output.name} onClose={() => setOrderOpen(false)} />}

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
        printSettings={model?.print_settings}
        project={{
          value: projectId,
          onChange: chooseProject,
          folder: folderChoice,
          list: projects,
          disabled: generating || creatingProject,
          onCreating: setDialogCreating,
        }}
      />
    </>
  )
}

/**
 * The other things Generate can make from the preview: an image to share, and (#1289)
 * the render broken down by colour. #968, #3672 — Radix's menu button: the arrows, Home,
 * End and Escape, focus back on the button as it closes.
 */
function GenerateMenu({
  disabled,
  onImage,
  onColours,
  onOrder,
}: {
  disabled: boolean
  onImage: () => void
  /** #4249 — once an output of the values on screen exists; it becomes a line item. */
  onOrder?: (() => void) | undefined
  /** Offered only when the render has colours to break down. */
  onColours?: (() => void) | undefined
}) {
  // The dialog opens once the menu has closed and put the focus back on its button, so
  // the dialog returns it there when it closes in turn. A task, not a microtask: Radix
  // returns the focus after the click's microtasks. Harmless if the bar unmounts first.
  const after = (open: () => void) => () => setTimeout(open, 0)
  const [open, setOpen] = useState(false)
  return (
    <DropdownMenu modal={false} open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          disabled={disabled}
          // The APG menu button opens on ArrowUp too; Radix's only on Enter, Space and ArrowDown.
          onKeyDown={(event) => {
            if (event.key === 'ArrowUp') {
              event.preventDefault()
              setOpen(true)
            }
          }}
          aria-label="More to generate"
          data-testid="generate-menu"
          className="h-9 rounded-l-none border-l border-l-primary-foreground/25 px-2"
        >
          <ChevronDownIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="top"
        align="end"
        aria-label="More to generate"
        // Tab leaves the menu closed, rather than kept in it (Radix holds it) or open behind.
        onKeyDown={(event) => {
          if (event.key === 'Tab') setOpen(false)
        }}
        className="w-auto min-w-56"
      >
        <DropdownMenuItem data-testid="generate-image" onSelect={after(onImage)} className="flex-col items-start gap-0">
          Rendered image…
          <span className="text-xs text-muted-foreground">A high-resolution PNG of the view</span>
        </DropdownMenuItem>
        {onColours && (
          <DropdownMenuItem data-testid="generate-colours" onSelect={after(onColours)} className="flex-col items-start gap-0">
            Colours…
            <span className="text-xs text-muted-foreground">Where each colour goes, one view per colour</span>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem data-testid="generate-order" disabled={!onOrder} onSelect={onOrder ? after(onOrder) : undefined} className="flex-col items-start gap-0">
          Add to order…
          <span className="text-xs text-muted-foreground">{onOrder ? 'This output as a line item of an order' : 'Generate first'}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
